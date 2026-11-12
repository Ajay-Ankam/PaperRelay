"""Shared per-request retry budget with deadline-aware full-jitter backoff.

delay(n) = max(retry_after, uniform(0, min(cap, base * 2**n))) where n counts retries
already used by the request (all stages and the retrieval tool share one budget). A retry
is skipped when `now + delay + min_attempt_s` would pass the request deadline: sleeping
only to time out helps nobody and still occupies a concurrency slot.

Jitter uses a per-request RNG seeded from (seed, request_id), so a run is reproducible
for the same request ids regardless of how concurrent requests interleave.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from paperrelay.providers.base import (
    CallCeilingReached,
    ProviderError,
    ProviderHTTPError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderTimeout,
)


@dataclass(frozen=True)
class RetrySettings:
    max_retries: int = 0  # 0 = retries off
    base_s: float = 0.5
    cap_s: float = 8.0
    seed: int = 0
    min_attempt_s: float = 1.0  # time an attempt needs to be worth starting (design choice)

    def public(self) -> dict[str, float | int]:
        return {"max": self.max_retries, "base_s": self.base_s, "cap_s": self.cap_s,
                "seed": self.seed, "min_attempt_s": self.min_attempt_s}


class RetryBudget:
    def __init__(self, settings: RetrySettings, request_id: str):
        self.settings = settings
        self.used = 0
        self._rng = random.Random(f"{settings.seed}:{request_id}")

    @property
    def remaining(self) -> int:
        return max(0, self.settings.max_retries - self.used)

    def delay_s(self, retry_after_s: float | None) -> float:
        s = self.settings
        jitter = self._rng.uniform(0.0, min(s.cap_s, s.base_s * (2 ** self.used)))
        return max(retry_after_s or 0.0, jitter)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date); None if absent/bad."""
    if value is None or not value.strip():
        return None
    try:
        seconds = float(value.strip())
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


_DURATION = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?$")
_MS = re.compile(r"^(\d+(?:\.\d+)?)ms$")


def parse_reset_duration(value: str | None) -> float | None:
    """Groq x-ratelimit-reset-* values such as '2m59.56s', '7.66s', '1h2m', '120ms'."""
    if not value:
        return None
    text = value.strip()
    ms = _MS.match(text)
    if ms:
        return float(ms.group(1)) / 1000
    match = _DURATION.match(text)
    if not match or not any(match.groups()):
        return None
    h, m, s = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + s


def default_retryable(err: ProviderError) -> bool:
    """Classification by error type, used when a provider did not classify the call."""
    if isinstance(err, CallCeilingReached | ProviderMalformed):
        return False
    if isinstance(err, ProviderRateLimited):
        return err.call.limit_scope != "day"
    if isinstance(err, ProviderTimeout):
        return True
    if isinstance(err, ProviderHTTPError):
        status = err.call.http_status
        return status is None or status == 200 or status >= 500
    return False


def is_retryable(err: ProviderError) -> bool:
    if isinstance(err, CallCeilingReached | ProviderMalformed):
        return False  # never retried, whatever a provider claims
    if err.call.retryable is not None:
        return err.call.retryable
    return default_retryable(err)
