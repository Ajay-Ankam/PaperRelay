"""Percentiles and small numeric helpers (no numpy dependency).

Percentile method: linear interpolation between closest ranks, identical to numpy's
default (`method="linear"`, Hyndman-Fan type 7): for sorted x of length n and p in [0,100],
h = (n-1)*p/100, result = x[floor(h)] + (h-floor(h)) * (x[floor(h)+1] - x[floor(h)]).
With small n, high percentiles are interpolated between the top observations and are
not reliable; tables print n next to every percentile and flag p95 below n=20 and
p99 below n=100.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

P95_MIN_N = 20
P99_MIN_N = 100


def percentile(values: Sequence[float], p: float) -> float | None:
    """numpy-default (linear) percentile; None for an empty sample (never 0)."""
    if not 0 <= p <= 100:
        raise ValueError("p must be within [0, 100]")
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    h = (len(xs) - 1) * p / 100.0
    lo = math.floor(h)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def low_n_flag(p: float, n: int) -> bool:
    """True when the sample is too small for the percentile to be meaningful."""
    if p >= 99:
        return n < P99_MIN_N
    if p >= 95:
        return n < P95_MIN_N
    return n == 0


def pct(part: int, whole: int) -> float | None:
    return None if whole == 0 else 100.0 * part / whole


def fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def to_float(raw: object) -> float | None:
    """CSV cell -> float; blank/None/non-numeric -> None (unknown, never 0)."""
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def known(values: Iterable[object]) -> list[float]:
    return [v for v in (to_float(x) for x in values) if v is not None]
