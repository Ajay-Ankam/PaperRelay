"""Stand-in simulator state: seeded timing draws, load slowdown, long tail, minute/day
limits with Groq-shaped 429s, injected 5xx and malformed streams.

Determinism: one RNG seeded from the config seed; every accepted call draws the same fixed
sequence of values, so a given arrival order of calls reproduces exactly. Concurrent
arrival order itself depends on the client and the OS scheduler.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from paperrelay.standin.config import StandinConfig

CONTENT_MODE = "synthetic-extractive"
WINDOW_S = 60.0


def format_duration(seconds: float) -> str:
    """Groq-style reset value, e.g. 7.66s, 2m59.56s, 1h2m3s."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    out = ""
    if h >= 1:
        out += f"{int(h)}h"
    if m >= 1 or h >= 1:
        out += f"{int(m)}m"
    return out + f"{round(s, 2):g}s"


@dataclass
class Rejection:
    status: int
    scope: str  # "minute" | "day" | "error"
    message: str
    type: str
    code: str
    retry_after_s: int | None = None


@dataclass
class Plan:
    """Everything decided for one accepted call before any bytes are sent."""

    call_no: int
    stage: str
    inflight_at_start: int
    slowdown: float
    tail: bool
    first_content_s: float
    stream_s: float
    prompt_tokens: int
    completion_tokens: int
    malformed: bool


@dataclass
class Simulator:
    config: StandinConfig
    calibration: dict[str, Any] | None = None
    clock: Any = time.perf_counter
    inflight: int = 0
    calls: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    _minute: deque[tuple[float, int]] = field(default_factory=deque)
    _day: str = ""
    day_requests: int = 0
    day_tokens: int = 0
    _rng: random.Random = field(default_factory=random.Random)
    applied_utc: str | None = None

    def __post_init__(self) -> None:
        self._rng = random.Random(self.config.seed)
        self._day = self.sim_now().date().isoformat()

    # ------------------------------------------------------------------ clocks

    def sim_now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.config.clock.day_offset_s)

    def _roll_day(self) -> None:
        today = self.sim_now().date().isoformat()
        if today != self._day:
            self._day, self.day_requests, self.day_tokens = today, 0, 0

    def _seconds_to_sim_midnight(self) -> float:
        now = self.sim_now()
        midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        return (midnight - now).total_seconds()

    def _prune(self) -> None:
        cutoff = self.clock() - WINDOW_S
        while self._minute and self._minute[0][0] <= cutoff:
            self._minute.popleft()

    def minute_usage(self) -> tuple[int, int]:
        self._prune()
        return len(self._minute), sum(t for _, t in self._minute)

    def _minute_free_in(self, tokens: int) -> tuple[float, float]:
        """Seconds until one more request fits / until `tokens` more fit."""
        self._prune()
        now, lim = self.clock(), self.config.limits
        req_wait = 0.0
        if len(self._minute) + 1 > lim.rpm:
            req_wait = self._minute[len(self._minute) - lim.rpm][0] + WINDOW_S - now
        tok_wait, total = 0.0, sum(t for _, t in self._minute)
        if total + tokens > lim.tpm:
            excess, freed = total + tokens - lim.tpm, 0
            for t0, tok in self._minute:
                freed += tok
                if freed >= excess:
                    tok_wait = t0 + WINDOW_S - now
                    break
            else:
                tok_wait = WINDOW_S  # larger than the whole limit: never fits
        return max(0.0, req_wait), max(0.0, tok_wait)

    # ------------------------------------------------------------------ headers

    def headers(self) -> dict[str, str]:
        """Groq semantics: *-requests = per-day request budget, *-tokens = per-minute tokens."""
        self._roll_day()
        lim = self.config.limits
        used_req, used_tok = self.minute_usage()
        oldest = self._minute[0][0] + WINDOW_S - self.clock() if self._minute else 0.0
        return {
            "x-ratelimit-limit-requests": str(lim.rpd),
            "x-ratelimit-remaining-requests": str(max(0, lim.rpd - self.day_requests)),
            "x-ratelimit-reset-requests": format_duration(
                self._seconds_to_sim_midnight() if self.day_requests else 0.0),
            "x-ratelimit-limit-tokens": str(lim.tpm),
            "x-ratelimit-remaining-tokens": str(max(0, lim.tpm - used_tok)),
            "x-ratelimit-reset-tokens": format_duration(max(0.0, oldest)),
            "x-standin-content": CONTENT_MODE,
        }

    # ------------------------------------------------------------------ admission

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def check_limits(self, tokens: int) -> Rejection | None:
        self._roll_day()
        lim, model = self.config.limits, self.config.model
        prefix = f"Rate limit reached for model `{model}` in organization `org_standin` " \
                 f"service tier `on_demand` on"
        if self.day_requests + 1 > lim.rpd:
            wait = math.ceil(self._seconds_to_sim_midnight())
            return Rejection(429, "day", f"{prefix} requests per day (RPD): Limit {lim.rpd}, "
                             f"Used {self.day_requests}, Requested 1. Please try again in "
                             f"{format_duration(wait)}.", "requests", "rate_limit_exceeded",
                             wait)
        if self.day_tokens + tokens > lim.tpd:
            wait = math.ceil(self._seconds_to_sim_midnight())
            return Rejection(429, "day", f"{prefix} tokens per day (TPD): Limit {lim.tpd}, "
                             f"Used {self.day_tokens}, Requested {tokens}. Please try again "
                             f"in {format_duration(wait)}.", "tokens", "rate_limit_exceeded",
                             wait)
        req_wait, tok_wait = self._minute_free_in(tokens)
        if req_wait > 0:
            used, _ = self.minute_usage()
            return Rejection(429, "minute", f"{prefix} requests per minute (RPM): Limit "
                             f"{lim.rpm}, Used {used}, Requested 1. Please try again in "
                             f"{format_duration(req_wait)}.", "requests",
                             "rate_limit_exceeded", max(1, math.ceil(req_wait)))
        if tok_wait > 0:
            _, used_tok = self.minute_usage()
            return Rejection(429, "minute", f"{prefix} tokens per minute (TPM): Limit "
                             f"{lim.tpm}, Used {used_tok}, Requested {tokens}. Please try "
                             f"again in {format_duration(tok_wait)}.", "tokens",
                             "rate_limit_exceeded", max(1, math.ceil(tok_wait)))
        return None

    def plan(self, stage: str, prompt_chars: int, max_completion: int,
             ) -> Rejection | Plan:
        """Decide the outcome of one call. Accepted calls are counted against limits."""
        self.calls += 1
        cfg = self.config
        prompt_tokens = math.ceil(prompt_chars / cfg.usage_chars_per_token)
        stage_cfg = cfg.latency.stages.get(stage) or cfg.latency.stages["draft"]
        completion = min(max_completion, stage_cfg.completion_tokens)
        tokens = prompt_tokens + completion
        rejection = self.check_limits(tokens)
        if rejection is not None:
            self._count(f"429_{rejection.scope}")
            return rejection
        # Fixed draw order per accepted call keeps runs reproducible for one arrival order.
        rng = self._rng
        u_error, u_malformed, u_tail = rng.random(), rng.random(), rng.random()
        z_first, z_stream = rng.gauss(0.0, 1.0), rng.gauss(0.0, 1.0)
        if u_error < cfg.faults.error_rate:
            self._count("error_injected")
            return Rejection(cfg.faults.error_status, "error",
                             "Service Unavailable (stand-in injected fault)",
                             "internal_server_error", "service_unavailable")
        self._minute.append((self.clock(), tokens))
        self.day_requests += 1
        self.day_tokens += tokens
        self.inflight += 1
        lat = cfg.latency
        slowdown = 1.0 + lat.slowdown_per_inflight * max(0, self.inflight - lat.slowdown_free_inflight)
        tail = u_tail < lat.tail_probability
        first = stage_cfg.first_content_median_ms * math.exp(stage_cfg.first_content_sigma * z_first)
        stream = stage_cfg.stream_median_ms * math.exp(stage_cfg.stream_sigma * z_stream)
        first *= slowdown * (lat.tail_multiplier if tail else 1.0)
        stream *= slowdown
        malformed = u_malformed < cfg.faults.malformed_rate
        self._count("malformed_injected" if malformed else "accepted")
        if tail:
            self._count("tail")
        return Plan(self.calls, stage, self.inflight, round(slowdown, 4), tail,
                    first * lat.time_scale / 1000, stream * lat.time_scale / 1000,
                    prompt_tokens, completion, malformed)

    def finish(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    # ------------------------------------------------------------------ admin

    def apply(self, config: StandinConfig, applied_utc: str) -> None:
        if config.seed != self.config.seed:
            self._rng = random.Random(config.seed)
        self.config = config
        self.applied_utc = applied_utc

    def set_day_usage(self, requests: int | None, tokens: int | None) -> None:
        self._roll_day()
        if requests is not None:
            self.day_requests = requests
        if tokens is not None:
            self.day_tokens = tokens

    def reset(self) -> None:
        self._minute.clear()
        self.day_requests = self.day_tokens = 0
        self._rng = random.Random(self.config.seed)
        self.calls = 0
        self.counters.clear()

    def state(self) -> dict[str, Any]:
        self._roll_day()
        req, tok = self.minute_usage()
        return {
            "content_mode": CONTENT_MODE,
            "content_note": "synthetic extractive JSON for pipeline mechanics only; not "
                            "model-quality evidence and not a replay of real outputs",
            "model": self.config.model,
            "seed": self.config.seed,
            "inflight": self.inflight,
            "calls_seen": self.calls,
            "counters": dict(self.counters),
            "minute": {"requests": req, "tokens": tok, "window_s": WINDOW_S},
            "day": {"sim_date_utc": self._day, "requests": self.day_requests,
                    "tokens": self.day_tokens},
            "sim_utc": self.sim_now().isoformat(),
            "config_applied_utc": self.applied_utc,
            "calibration": {
                "path": self.config.calibration,
                "version": (self.calibration or {}).get("version"),
                "preliminary": (self.calibration or {}).get("preliminary"),
            },
            "config": self.config.model_dump(),
        }
