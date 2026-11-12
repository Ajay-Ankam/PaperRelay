"""Streaming /ask client shared by the load runner and the evaluation runner.

Differences from paperrelay.client.smoke (which stays unchanged):
- request ids are supplied by the caller (deterministic from the seed);
- client abandonment is a wall-clock deadline over the whole request
  (asyncio.timeout), not httpx's per-read timeout: progress events would otherwise keep a
  stalled request alive forever;
- the full terminal payload is kept (answer, citations, capacity accounting).

Row construction reuses smoke._row for the shared columns and then fills the load/
capacity extension columns from the terminal payload or, for requests whose terminal
event never reached the client, from the server's requests.jsonl record.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from paperrelay.client.smoke import _row

# Terminal NDJSON kinds. "rate_limited" is accepted in case the server emits it as its own
# kind; the status is always read from payload.status.
TERMINAL_KINDS = frozenset({"done", "refused", "error", "timeout", "cancelled", "rate_limited"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def stream_ask(client: httpx.AsyncClient, *, request_id: str, question: str,
                     retrieval_setting: str, timeout_s: float,
                     t_ref: float | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Send one /ask and observe it until a terminal event, an error or abandonment.

    Returns (observation, terminal_payload|None). All times are client monotonic
    (time.perf_counter) in ms from the send instant.
    """
    obs: dict[str, Any] = {
        "request_id": request_id, "question": question, "retrieval_setting": retrieval_setting,
        "first_token_ms": None, "first_progress_ms": None, "events": [], "parse_errors": [],
    }
    terminal: dict[str, Any] | None = None
    body = {"question": question, "retrieval_setting": retrieval_setting,
            "request_id": request_id}
    t0 = time.perf_counter()
    obs["start_time"] = utc_now()
    obs["send_perf_s"] = t0 - (t_ref if t_ref is not None else t0)
    try:
        async with asyncio.timeout(timeout_s):
            async with client.stream("POST", "/ask", json=body) as resp:
                obs["http_status"] = resp.status_code
                if resp.status_code != 200:
                    obs["http_error"] = f"{resp.status_code}: {(await resp.aread())[:200]!r}"
                    obs["client_reason"] = f"http_{resp.status_code}"
                else:
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        elapsed = (time.perf_counter() - t0) * 1000
                        try:
                            event = json.loads(line)
                            kind = event["kind"]
                        except (ValueError, KeyError, TypeError) as exc:
                            obs["parse_errors"].append({"client_ms": round(elapsed, 3),
                                                        "error": type(exc).__name__,
                                                        "line": line[:200]})
                            continue
                        obs["events"].append({"kind": kind, "client_ms": round(elapsed, 3)})
                        if kind == "answer_token":
                            if obs["first_token_ms"] is None:
                                obs["first_token_ms"] = elapsed
                        elif obs["first_progress_ms"] is None:
                            obs["first_progress_ms"] = elapsed
                        if kind in TERMINAL_KINDS:
                            terminal = event.get("payload") or {}
                            break
                    else:
                        obs["client_reason"] = "stream_ended_without_terminal"
    except TimeoutError:
        # Leaving the context closes the connection; the server cancels the pipeline.
        obs["http_error"] = "client_deadline"
        obs["client_reason"] = "client_abandoned"
    except httpx.TimeoutException as exc:
        obs["http_error"] = type(exc).__name__
        obs["client_reason"] = "client_abandoned"
    except httpx.HTTPError as exc:
        obs["http_error"] = type(exc).__name__
        obs["client_reason"] = f"client_transport_error:{type(exc).__name__}"
    obs["total_ms"] = (time.perf_counter() - t0) * 1000
    return obs, terminal


# ---------------------------------------------------------------------------
# Server evidence join
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                continue  # partially written last line; the server owns this file
    return items


def server_records(run_dir: Path) -> dict[str, dict[str, Any]]:
    """request_id -> last requests.jsonl record, with its calls attached."""
    records: dict[str, dict[str, Any]] = {}
    for item in read_jsonl(run_dir / "requests.jsonl"):
        rid = item.get("request_id")
        if isinstance(rid, str):
            records[rid] = dict(item)
    calls: dict[str, list[dict[str, Any]]] = {}
    for call in read_jsonl(run_dir / "calls.jsonl"):
        calls.setdefault(str(call.get("request_id")), []).append(call)
    for rid, rec in records.items():
        rec["calls"] = calls.get(rid, [])
    return records


async def await_server_records(run_dir: Path, wanted: set[str],
                               wait_s: float) -> dict[str, dict[str, Any]]:
    """Abandoned requests are written by the server only after cancellation completes,
    so poll until every wanted id is present or the wait expires."""
    deadline = time.monotonic() + wait_s
    while True:
        records = server_records(run_dir)
        if wanted <= set(records) or time.monotonic() >= deadline:
            return records
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# results.csv rows
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def build_row(obs: dict[str, Any], terminal: dict[str, Any] | None, meta: dict[str, Any],
              server: dict[str, Any] | None, *, scenario: str, question_id: str,
              extra: dict[str, Any],
              diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    """One results.csv row (smoke.COLUMNS order is applied by the CSV writer).

    `extra` carries run-level fields: run_id, seed, scheduled_start_time,
    offered_window_s, arrival_lag_ms, pipeline_commit/config_hash fallbacks.
    `diagnostics`, if given, receives partition_inconsistent (and the raw components when
    true) so the caller can record it; the CSV schema is unchanged.
    """
    obs = {**obs, "scenario": scenario, "question_id": question_id}
    if terminal is None and server is None and obs.get("client_reason") == "http_429":
        # Admission refusal returned as an HTTP status rather than an NDJSON terminal: no
        # pipeline ran and no provider call was made, so zero tokens is known, not assumed.
        terminal = {"status": "rate_limited", "reason": "http_429", "provider_calls": 0,
                    "unknown_usage_calls": 0, "tokens_in": 0, "tokens_out": 0}
    row = _row(obs, terminal, meta, server)
    acct = terminal if terminal else (server or {})
    retries = acct.get("retries")
    queue_wait = _num(acct.get("queue_wait_ms"))
    rate_wait = _num(acct.get("rate_wait_ms"))
    retry_wait = _num(acct.get("retry_wait_ms"))
    waiting = None if queue_wait is None and rate_wait is None else (
        (queue_wait or 0.0) + (rate_wait or 0.0))
    total = float(obs["total_ms"])
    # working = client total - waiting - retry wait (so the three partition the client
    # total, with client/network overhead classified as working). Blank if the server
    # reported neither component: it cannot be derived without them.
    working = None
    inconsistent = False
    if waiting is not None or retry_wait is not None:
        working = round(total - (waiting or 0.0) - (retry_wait or 0.0), 1)
        if working < 0:
            # Server-side waits exceed the client's total (e.g. an abandoned request whose
            # queue wait kept running until the server noticed the disconnect). The three
            # cannot partition the client total; working is left blank (not derivable),
            # never clamped to 0, and the inconsistency is reported to the caller.
            inconsistent = True
            if diagnostics is not None:
                diagnostics.update({"working_ms_raw": working, "client_total_ms": total,
                                    "waiting_ms": waiting, "retry_wait_ms": retry_wait})
            working = None
    if diagnostics is not None:
        diagnostics["partition_inconsistent"] = inconsistent
    row.update({
        "run_id": extra.get("run_id", row["run_id"]),
        "seed": extra.get("seed", ""),
        "scheduled_start_time": extra.get("scheduled_start_time", ""),
        "actual_send_time": obs["start_time"],
        "offered_window_s": extra.get("offered_window_s", ""),
        "arrival_lag_ms": extra.get("arrival_lag_ms", ""),
        # Blank when the server did not report it: unknown is never 0.
        "retries": retries if isinstance(retries, int) else "",
        "queue_peak_observed": acct.get("queue_peak_observed")
        if acct.get("queue_peak_observed") is not None else "",
        "waiting_ms": "" if waiting is None else round(waiting, 1),
        "retry_wait_ms": "" if retry_wait is None else round(retry_wait, 1),
        "working_ms": "" if working is None else working,
        "pipeline_commit": row["pipeline_commit"] or extra.get("pipeline_commit", ""),
        "config_hash": row["config_hash"] or extra.get("config_hash", ""),
        "cost_basis": extra.get("cost_basis", row["cost_basis"]),
    })
    return row


def not_sent_row(*, request_id: str, scenario: str, question_id: str, retrieval_setting: str,
                 reason: str, meta: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Row for a scheduled request that was never sent (runner cap or cancellation):
    no send time, no total time, status error, explicit reason."""
    obs = {"request_id": request_id, "scenario": scenario, "question_id": question_id,
           "retrieval_setting": retrieval_setting, "start_time": "", "first_token_ms": None,
           "total_ms": 0.0, "client_reason": reason}
    row = _row(obs, None, meta, None)
    row.update({
        "total_ms": "", "status": "error", "outcome_reason": reason, "retries": "",
        "retry_wait_ms": "", "actual_send_time": "", "arrival_lag_ms": "",
        "run_id": extra.get("run_id", ""), "seed": extra.get("seed", ""),
        "scheduled_start_time": extra.get("scheduled_start_time", ""),
        "offered_window_s": extra.get("offered_window_s", ""),
        "pipeline_commit": extra.get("pipeline_commit", ""),
        "config_hash": extra.get("config_hash", ""),
        "cost_basis": extra.get("cost_basis", row["cost_basis"]),
    })
    return row
