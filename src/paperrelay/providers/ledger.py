"""Persistent hard ceiling on real provider calls.

One ledger file per evidence root (evidence/real/call_ledger.json) holds every run's
budget and usage. A run's stored ceiling can only be lowered from the environment, never
raised; a new budget needs a new, deliberately chosen run id, and all runs stay visible
in the same file. Writes are atomic (temp file + os.replace). The process holds an OS
lock on call_ledger.lock while running, so a second worker or server refuses to start.
Attempts are counted when reserved, before any network I/O, so the count is conservative.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from paperrelay.contracts import Source
from paperrelay.providers.base import (
    CallCeilingReached,
    CallContext,
    ChatProvider,
    ChatRequest,
    ChatResult,
    make_call,
)


class LedgerError(RuntimeError):
    pass


def _lock(fh: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise LedgerError("call ledger is locked by another running server process") from exc


class CallLedger:
    def __init__(self, path: Path, run_id: str, ceiling: int, hold_lock: bool = True):
        self.path = path
        self.run_id = run_id
        self._holds: dict[str, int] = {}
        self._lock_fh: IO[bytes] | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        if hold_lock:
            self._lock_fh = open(path.with_suffix(".lock"), "a+b")
            _lock(self._lock_fh)
        data = self._read()
        run = data["runs"].get(run_id)
        stored = int(run["ceiling"]) if run else ceiling
        self.ceiling: int = min(stored, ceiling)
        data["runs"][run_id] = {
            "ceiling": self.ceiling,
            "used": run["used"] if run else 0,
            "created_utc": run["created_utc"] if run else datetime.now(UTC).isoformat(),
        }
        self._write(data)

    def close(self) -> None:
        if self._lock_fh is not None:
            self._lock_fh.close()
            self._lock_fh = None

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"runs": {}}
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LedgerError(f"{self.path} is unreadable; inspect it by hand") from exc
        data.setdefault("runs", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["total_used"] = sum(r["used"] for r in data["runs"].values())
        data["updated_utc"] = datetime.now(UTC).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def used(self) -> int:
        return int(self._read()["runs"][self.run_id]["used"])

    def remaining(self) -> int:
        return max(0, self.ceiling - self.used())

    def admit(self, request_id: str, calls: int) -> bool:
        """Hold `calls` slots for a request so concurrent requests cannot oversubscribe."""
        if self.remaining() - sum(self._holds.values()) < calls:
            return False
        self._holds[request_id] = calls
        return True

    def release(self, request_id: str) -> None:
        self._holds.pop(request_id, None)

    def reserve(self, request_id: str) -> bool:
        data = self._read()
        run = data["runs"][self.run_id]
        if run["used"] >= self.ceiling:
            return False
        held = self._holds.get(request_id)
        if held is None and run["used"] + sum(self._holds.values()) >= self.ceiling:
            return False  # unadmitted caller may not consume another request's held slots
        run["used"] += 1
        self._write(data)
        if held is not None:
            self._holds[request_id] = max(0, held - 1)
        return True


class CeilingGuardProvider:
    """Wraps a real provider; refuses locally (no network I/O) once the ceiling is reached."""

    def __init__(self, inner: ChatProvider, ledger: CallLedger):
        self.inner = inner
        self.ledger = ledger
        self.name = inner.name
        self.source: Source = inner.source
        self.model = inner.model

    async def complete(self, request: ChatRequest, ctx: CallContext) -> ChatResult:
        if not self.ledger.reserve(ctx.request_id):
            call = make_call(
                self, request, ctx, status="refused_by_ceiling", t_send=time.perf_counter(),
                streamed=False, sent=False,
                error=f"real call ceiling {self.ledger.ceiling} reached",
            )
            ctx.record(call)
            raise CallCeilingReached(call, call.error or "ceiling reached")
        return await self.inner.complete(request, ctx)
