"""Server-side evidence: call-level, event-level and per-request JSONL.

Layout: <evidence_dir>/<source>/<run_id>/{run_meta.json, calls.jsonl, events.jsonl,
requests.jsonl}. results.csv in the same folder is written only by the client runner, so
each sent request appears there exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class EvidenceLogger:
    def __init__(self, root: Path, source: str, run_id: str):
        self.dir = root / source / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, record: dict[str, Any]) -> None:
        with open(self.dir / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def write_meta(self, meta: dict[str, Any]) -> None:
        (self.dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )

    def call(self, call: BaseModel) -> None:
        self._append("calls.jsonl", call.model_dump(mode="json"))

    def event(self, event: BaseModel, utc: str) -> None:
        self._append("events.jsonl", {**event.model_dump(mode="json"), "utc": utc})

    def request(self, record: dict[str, Any]) -> None:
        self._append("requests.jsonl", record)
