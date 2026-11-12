"""Provider-call quota gate: sliding minute window plus a persisted UTC-day window.

Every provider attempt reserves 1 request and an estimated token count *before* any
network I/O, in both windows. After the attempt the reservation is reconciled:
- attempt never sent (connect failure, local refusal): request and tokens refunded;
- provider reported prompt+completion usage: tokens corrected to the reported total;
- usage unknown (cancelled, timed out, 429, malformed, missing usage): the reservation is
  kept as the best available upper-bound estimate (never treated as zero).

Minute window: sliding `window_s` (60 s) over this process's own reservations. A shortfall
waits (FIFO via one lock) when the wait fits the request deadline, else it is rejected.
Day window: calendar UTC day, persisted atomically to a JSON file after every change so a
restart does not forget spend; a new UTC date resets the counters. Groq's own daily limits
replenish on a rolling basis (see reset headers), so this calendar-day window is a local
policy, not a model of Groq's accounting. Upstream day-scope 429s block further calls until
the advertised reset (or the next UTC day if none is given).

Admission holds: an admitted pipeline request holds its worst-case call count in the day
window so concurrent requests cannot jointly overrun the day budget mid-pipeline.

Single process, single event loop only. Usage by other processes/keys is invisible here.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from paperrelay.contracts import ProviderCall

MINUTE_REQUESTS = "minute_requests"
MINUTE_TOKENS = "minute_tokens"
GATE_QUEUE = "gate_queue"  # waiting behind earlier callers that are waiting for the window


class QuotaRejected(Exception):
    def __init__(self, reason: str, scope: str):
        super().__init__(reason)
        self.reason = reason
        self.scope = scope  # "minute" | "day"


class QuotaStateError(RuntimeError):
    pass


@dataclass
class Limits:
    rpm: int
    tpm: int
    rpd: int
    tpd: int

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"limit {name} must be a non-negative integer")


@dataclass
class _Entry:
    t: float
    tokens: int
    requests: int = 1


@dataclass
class Reservation:
    request_id: str
    tokens: int
    day: str
    entry: _Entry
    settled: bool = False


def estimate_tokens(messages: list[dict[str, str]], max_completion_tokens: int,
                    chars_per_token: float) -> int:
    """Conservative pre-call estimate: prompt chars / chars_per_token (+4 per message for
    role framing) plus the full completion allowance. Not a tokenizer."""
    chars = sum(len(m.get("content", "")) for m in messages)
    prompt = math.ceil(chars / chars_per_token) + 4 * len(messages)
    return prompt + max_completion_tokens


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class QuotaGate:
    limits: Limits
    state_path: Path | None = None
    window_s: float = 60.0
    clock: Callable[[], float] = time.perf_counter
    utc_now: Callable[[], datetime] = _utc_now
    # day state
    day: str = ""
    day_requests: int = 0
    day_tokens: int = 0
    blocked_until_utc: datetime | None = None
    blocked_reason: str | None = None
    unknown_usage_kept: int = 0
    _holds: dict[str, int] = field(default_factory=dict)
    _window: deque[_Entry] = field(default_factory=deque)
    _lock: asyncio.Lock | None = None
    _changed: asyncio.Event | None = None
    limits_applied_utc: str | None = None

    def __post_init__(self) -> None:
        self.limits.validate()
        self.day = self.utc_now().date().isoformat()
        if self.state_path is not None:
            self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self.state_path is not None
        if not self.state_path.exists():
            self._persist()
            return
        try:
            data: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
            stored_day = str(data["date_utc"])
            requests, tokens = int(data["requests"]), int(data["tokens"])
            blocked = data.get("blocked_until_utc")
        except (ValueError, KeyError, TypeError) as exc:
            # Never silently reset spend: a corrupt file needs a human decision.
            raise QuotaStateError(f"{self.state_path} is unreadable; inspect it by hand") from exc
        if stored_day == self.day:
            self.day_requests, self.day_tokens = requests, tokens
            if blocked:
                self.blocked_until_utc = datetime.fromisoformat(blocked)
                self.blocked_reason = data.get("blocked_reason")
        self._persist()

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": 1,
            "scope": "this API process only; UTC calendar day; reservations are counted "
                     "before the call and reconciled to reported usage afterwards",
            "date_utc": self.day,
            "requests": self.day_requests,
            "tokens": self.day_tokens,
            "limits": asdict(self.limits),
            "blocked_until_utc": (self.blocked_until_utc.isoformat()
                                  if self.blocked_until_utc else None),
            "blocked_reason": self.blocked_reason,
            "updated_utc": self.utc_now().isoformat(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ windows

    def _roll_day(self) -> None:
        today = self.utc_now().date().isoformat()
        if today != self.day:
            self.day, self.day_requests, self.day_tokens = today, 0, 0
            if self.blocked_until_utc and self.blocked_until_utc <= self.utc_now():
                self.blocked_until_utc, self.blocked_reason = None, None
            self._persist()

    def _prune(self) -> None:
        cutoff = self.clock() - self.window_s
        while self._window and self._window[0].t <= cutoff:
            self._window.popleft()

    def minute_usage(self) -> tuple[int, int]:
        self._prune()
        return (sum(e.requests for e in self._window), sum(e.tokens for e in self._window))

    def _minute_wait(self, tokens: int) -> tuple[float, str]:
        self._prune()
        now = self.clock()
        live = [e for e in self._window if e.requests or e.tokens]
        req_wait = tok_wait = 0.0
        counted = [e for e in live if e.requests]
        if len(counted) + 1 > self.limits.rpm:
            oldest_needed = counted[len(counted) - self.limits.rpm]
            req_wait = oldest_needed.t + self.window_s - now
        total = sum(e.tokens for e in live)
        if total + tokens > self.limits.tpm:
            excess, freed = total + tokens - self.limits.tpm, 0
            for e in live:
                freed += e.tokens
                if freed >= excess:
                    tok_wait = e.t + self.window_s - now
                    break
        if req_wait <= 0 and tok_wait <= 0:
            return 0.0, ""
        if req_wait >= tok_wait:
            return req_wait + 0.001, MINUTE_REQUESTS
        return tok_wait + 0.001, MINUTE_TOKENS

    def _day_blocked(self) -> bool:
        if self.blocked_until_utc is None:
            return False
        if self.blocked_until_utc <= self.utc_now():
            self.blocked_until_utc, self.blocked_reason = None, None
            self._persist()
            return False
        return True

    def _day_shortfall(self, request_id: str, tokens: int) -> bool:
        self._roll_day()
        if self._day_blocked():
            return True
        others = sum(v for k, v in self._holds.items() if k != request_id)
        if self.day_requests + 1 + others > self.limits.rpd:
            return True
        return self.day_tokens + tokens > self.limits.tpd

    # ------------------------------------------------------------------ admission holds

    def admit(self, request_id: str, calls: int) -> bool:
        """Hold `calls` day-window requests for a pipeline request (released by release())."""
        self._roll_day()
        if self._day_blocked() or self.day_tokens >= self.limits.tpd:
            return False
        if self.day_requests + sum(self._holds.values()) + calls > self.limits.rpd:
            return False
        self._holds[request_id] = calls
        return True

    def release(self, request_id: str) -> None:
        self._holds.pop(request_id, None)

    def day_exhausted(self) -> bool:
        self._roll_day()
        return (self._day_blocked() or self.day_requests >= self.limits.rpd
                or self.day_tokens >= self.limits.tpd)

    # ------------------------------------------------------------------ reservations

    async def reserve(
        self, request_id: str, tokens: int, deadline_at: float,
        on_wait: Callable[[float, str], None] | None = None,
    ) -> Reservation:
        """Reserve 1 request + `tokens` in both windows. Raises QuotaRejected."""
        if self._day_shortfall(request_id, tokens):
            raise QuotaRejected("daily_budget_exhausted", "day")
        if tokens > self.limits.tpm or self.limits.rpm < 1:
            raise QuotaRejected("call_estimate_exceeds_minute_limit", "minute")
        if self._lock is None:
            self._lock = asyncio.Lock()
        # A just-released lock may already be promised to a woken waiter (FIFO), so pending
        # waiters also count as contention.
        contended = self._lock.locked() or bool(getattr(self._lock, "_waiters", None))
        if not contended:
            await self._lock.acquire()  # uncontended: returns without yielding
        else:
            if on_wait is not None:
                on_wait(0.0, GATE_QUEUE)  # another caller is already waiting for the window
            try:
                async with asyncio.timeout(max(0.0, deadline_at - self.clock())):
                    await self._lock.acquire()
            except TimeoutError:
                raise QuotaRejected("minute_budget_wait_exceeds_deadline", "minute") from None
        try:
            while True:
                if self._day_shortfall(request_id, tokens):
                    raise QuotaRejected("daily_budget_exhausted", "day")
                if tokens > self.limits.tpm or self.limits.rpm < 1:
                    raise QuotaRejected("call_estimate_exceeds_minute_limit", "minute")
                wait, scope = self._minute_wait(tokens)
                if wait <= 0:
                    break
                if self.clock() + wait > deadline_at:
                    raise QuotaRejected("minute_budget_wait_exceeds_deadline", "minute")
                if on_wait is not None:
                    on_wait(wait, scope)
                if self._changed is None:
                    self._changed = asyncio.Event()
                changed = self._changed
                try:  # wake early if limits change (e.g. raised by an operator)
                    async with asyncio.timeout(wait):
                        await changed.wait()
                except TimeoutError:
                    pass
            entry = _Entry(self.clock(), tokens)
            self._window.append(entry)
            self.day_requests += 1
            self.day_tokens += tokens
            if request_id in self._holds:
                self._holds[request_id] = max(0, self._holds[request_id] - 1)
            self._persist()
            return Reservation(request_id, tokens, self.day, entry)
        finally:
            self._lock.release()

    def reconcile(self, res: Reservation, call: ProviderCall | None) -> int:
        """Settle a reservation with the attempt's record. Returns the token delta applied."""
        if res.settled:
            return 0
        res.settled = True
        same_day = res.day == self.day
        delta = 0
        if call is not None and not call.sent:
            delta = -res.entry.tokens
            res.entry.tokens, res.entry.requests = 0, 0
            if same_day:
                self.day_requests = max(0, self.day_requests - 1)
                self.day_tokens = max(0, self.day_tokens + delta)
        elif call is not None and call.usage.complete:
            assert call.usage.prompt_tokens is not None
            assert call.usage.completion_tokens is not None
            actual = call.usage.prompt_tokens + call.usage.completion_tokens
            delta = actual - res.entry.tokens
            res.entry.tokens = actual
            if same_day:
                self.day_tokens = max(0, self.day_tokens + delta)
        else:
            self.unknown_usage_kept += 1
        self._persist()
        return delta

    # ------------------------------------------------------------------ control

    def set_limits(self, rpm: int | None = None, tpm: int | None = None,
                   rpd: int | None = None, tpd: int | None = None) -> Limits:
        new = Limits(
            rpm=self.limits.rpm if rpm is None else rpm,
            tpm=self.limits.tpm if tpm is None else tpm,
            rpd=self.limits.rpd if rpd is None else rpd,
            tpd=self.limits.tpd if tpd is None else tpd,
        )
        new.validate()
        self.limits = new
        self.limits_applied_utc = self.utc_now().isoformat()
        self._persist()
        if self._changed is not None:
            self._changed.set()
            self._changed = asyncio.Event()
        return new

    def mark_upstream_day_exhausted(self, until_utc: datetime | None, reason: str) -> None:
        if until_utc is None:
            now = self.utc_now()
            until_utc = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        if self.blocked_until_utc is None or until_utc > self.blocked_until_utc:
            self.blocked_until_utc = until_utc
        self.blocked_reason = reason
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        self._roll_day()
        requests, tokens = self.minute_usage()
        return {
            "limits": asdict(self.limits),
            "limits_applied_utc": self.limits_applied_utc,
            "minute": {"requests": requests, "tokens": tokens, "window_s": self.window_s},
            "day": {
                "date_utc": self.day, "requests": self.day_requests, "tokens": self.day_tokens,
                "held_requests": sum(self._holds.values()),
                "blocked_until_utc": (self.blocked_until_utc.isoformat()
                                      if self.blocked_until_utc else None),
                "blocked_reason": self.blocked_reason,
                "persisted_to": str(self.state_path) if self.state_path else None,
            },
            "unknown_usage_reservations_kept": self.unknown_usage_kept,
        }
