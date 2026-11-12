"""Deterministic retrieval-tool fault injection (PAPERRELAY_FAULT_RETRIEVAL).

Spec: comma-separated `key=value`, e.g. `timeout=0.1,error=0.05,seed=7`. For each tool
attempt a uniform draw u in [0,1) is derived from sha256(seed:request_id:attempt); u <
timeout -> the attempt hangs past the tool timeout; u < timeout+error -> the tool raises.
Same seed + request id + attempt -> same fault, independent of concurrency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

Fault = Literal["timeout", "error"]


class InjectedToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrievalFaults:
    timeout: float = 0.0
    error: float = 0.0
    seed: int = 0

    @classmethod
    def parse(cls, spec: str) -> RetrievalFaults:
        values: dict[str, float] = {}
        for part in (p.strip() for p in spec.split(",")):
            if not part:
                continue
            key, sep, raw = part.partition("=")
            key = key.strip()
            if not sep or key not in ("timeout", "error", "seed"):
                raise ValueError(f"bad fault spec element {part!r}")
            values[key] = float(raw)
        faults = cls(timeout=values.get("timeout", 0.0), error=values.get("error", 0.0),
                     seed=int(values.get("seed", 0)))
        if not (0 <= faults.timeout <= 1 and 0 <= faults.error <= 1
                and faults.timeout + faults.error <= 1):
            raise ValueError("fault rates must be in [0,1] and sum to at most 1")
        return faults

    @property
    def active(self) -> bool:
        return self.timeout > 0 or self.error > 0

    def draw(self, request_id: str, attempt: int) -> float:
        digest = hashlib.sha256(f"{self.seed}:{request_id}:{attempt}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def decide(self, request_id: str, attempt: int) -> Fault | None:
        if not self.active:
            return None
        u = self.draw(request_id, attempt)
        if u < self.timeout:
            return "timeout"
        if u < self.timeout + self.error:
            return "error"
        return None

    def spec(self) -> str:
        return f"timeout={self.timeout},error={self.error},seed={self.seed}"
