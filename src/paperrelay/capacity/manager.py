"""Bundles the admission controller and quota gate that one API process shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paperrelay.capacity.admission import AdmissionController
from paperrelay.capacity.quota import QuotaGate

DAILY_EXHAUSTED = "daily_budget_exhausted"


@dataclass
class Capacity:
    admission: AdmissionController
    quota: QuotaGate
    chars_per_token: float = 3.0
    queued_refused_on_day_exhaustion: int = 0

    def on_day_exhausted(self) -> int:
        """Refuse every queued request: none of them can be served today."""
        failed = self.admission.fail_queued(DAILY_EXHAUSTED)
        self.queued_refused_on_day_exhaustion += failed
        return failed

    def snapshot(self, retry: dict[str, Any] | None = None) -> dict[str, Any]:
        q = self.quota.snapshot()
        return {
            "queue_depth": self.admission.queue_depth,
            "in_flight": self.admission.in_flight,
            "max_concurrency": self.admission.max_concurrency,
            "max_queue": self.admission.max_queue,
            "queue_peak_process": self.admission.peak_queue_depth,
            "limits": q["limits"],
            "limits_applied_utc": q["limits_applied_utc"],
            "minute": q["minute"],
            "day": q["day"],
            "retry": retry or {},
            "chars_per_token": self.chars_per_token,
            "unknown_usage_reservations_kept": q["unknown_usage_reservations_kept"],
            "queued_refused_on_day_exhaustion": self.queued_refused_on_day_exhaustion,
            "coordination": "single process (in-memory); not shared across workers",
        }
