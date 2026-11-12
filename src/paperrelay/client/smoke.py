"""Client-side smoke runner: sends questions to /ask and records client-observed timing.

first_token_ms is the client monotonic time from send until the first answer_token event
(checked answer text) arrives; progress events never count. total_ms runs until the
terminal event or connection end. This runner is the only writer of results.csv, so every
sent request appears there exactly once; server JSONL files are audit evidence joined on
request_id.

Usage (PowerShell, server already running):
    .\\.venv\\Scripts\\python.exe -m paperrelay.client.smoke --question "..." --max-requests 2
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REQUIRED_COLUMNS = [
    "request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status",
    "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
]
EXTENSION_COLUMNS = [
    "run_id", "question_id", "model", "pipeline_commit", "config_hash", "seed",
    "scheduled_start_time", "actual_send_time", "offered_window_s", "arrival_lag_ms",
    "outcome_reason", "queue_peak_observed", "tokens_usage_complete",
    "provider_first_token_ms", "waiting_ms", "working_ms", "retry_wait_ms",
    "answer_delivery_ms", "cost_usd", "cost_basis", "limit_snapshot_json",
]
COLUMNS = REQUIRED_COLUMNS + EXTENSION_COLUMNS

# results.csv status vocabulary is transport-level: a correct refusal is still "ok".
_STATUS = {"answered": "ok", "refused": "ok", "rate_limited": "rate_limited",
           "timeout": "timeout", "cancelled": "timeout", "error": "error"}


def _tokens(u: dict[str, Any] | None) -> tuple[Any, Any, Any]:
    """(tokens_in, tokens_out, usage_complete). Unknown usage is blank, never 0; partial
    knowledge keeps the known sum and is flagged by usage_complete=False."""
    if u is None:
        return "", "", ""
    calls, unknown = u.get("provider_calls", 0), u.get("unknown_usage_calls", 0)
    if calls and unknown >= calls:
        return "", "", False
    return u.get("tokens_in", ""), u.get("tokens_out", ""), unknown == 0


def _row(obs: dict[str, Any], terminal: dict[str, Any] | None, meta: dict[str, Any],
         server: dict[str, Any] | None) -> dict[str, Any]:
    t = terminal or {}
    first = obs["first_token_ms"]
    if terminal:
        status = _STATUS.get(t.get("status", ""), "error")
        reason = t.get("reason") or ""
    else:
        # No terminal event reached the client. A client-side timeout is an abandonment.
        status = "timeout" if obs.get("client_reason") == "client_abandoned" else "error"
        reason = obs.get("client_reason") or "no_terminal_event"
    calls = (server or {}).get("calls", [])
    first_call = next((c for c in calls if c.get("sent")), None)
    limits = next((c["rate_limit_headers"] for c in reversed(calls) if c.get("rate_limit_headers")),
                  None)
    # Every provider call counts toward tokens, even when the client never saw a terminal
    # event: fall back to the server's request record for the same request_id.
    tokens_in, tokens_out, usage_complete = _tokens(terminal if terminal else server)
    pft = (first_call or {}).get("provider_first_content_ms")
    return {
        "request_id": obs["request_id"],
        "scenario": obs["scenario"],
        "source": t.get("source", meta.get("source", "")),
        "start_time": obs["start_time"],
        "first_token_ms": "" if first is None else round(first, 1),
        "total_ms": round(obs["total_ms"], 1),
        "status": status,
        "retries": 0,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "retrieval_setting": t.get("retrieval_setting", obs["retrieval_setting"]),
        "answer_grade": "",
        "run_id": meta.get("run_id", ""),
        "question_id": obs["question_id"],
        "model": t.get("model", meta.get("model", "")),
        "pipeline_commit": (server or {}).get("pipeline_commit", ""),
        "config_hash": (server or {}).get("config_hash", ""),
        "seed": "",
        "scheduled_start_time": "",
        "actual_send_time": obs["start_time"],
        "offered_window_s": "",
        "arrival_lag_ms": "",
        "outcome_reason": reason,
        "queue_peak_observed": "",
        "tokens_usage_complete": usage_complete,
        # Provider TTFT of the first sent call of the request (the interpret stage),
        # measured server-side from that call's send. Not the user-facing first_token_ms.
        "provider_first_token_ms": "" if pft is None else pft,
        "waiting_ms": "",
        "working_ms": t.get("server_total_ms", (server or {}).get("server_total_ms", "")),
        "retry_wait_ms": 0,
        "answer_delivery_ms": (
            round(obs["total_ms"] - first, 1) if first is not None else ""
        ),
        "cost_usd": "",
        "cost_basis": "not_measured",
        "limit_snapshot_json": json.dumps(limits) if limits else "",
    }


async def ask_once(client: httpx.AsyncClient, question: str, retrieval_setting: str,
                   scenario: str, question_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    request_id = uuid.uuid4().hex
    obs: dict[str, Any] = {
        "request_id": request_id, "scenario": scenario, "question_id": question_id,
        "retrieval_setting": retrieval_setting, "question": question,
        "start_time": datetime.now(UTC).isoformat(), "first_token_ms": None,
        "first_progress_ms": None, "events": [],
    }
    terminal: dict[str, Any] | None = None
    t0 = time.perf_counter()
    body = {"question": question, "retrieval_setting": retrieval_setting, "request_id": request_id}
    obs["parse_errors"] = []
    try:
        async with client.stream("POST", "/ask", json=body) as resp:
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
                        obs["parse_errors"].append(
                            {"client_ms": round(elapsed, 3), "error": type(exc).__name__,
                             "line": line[:200]})
                        continue
                    obs["events"].append({"kind": kind, "client_ms": round(elapsed, 3)})
                    if kind == "answer_token":
                        if obs["first_token_ms"] is None:
                            obs["first_token_ms"] = elapsed
                    elif obs["first_progress_ms"] is None:
                        obs["first_progress_ms"] = elapsed
                    if kind in ("done", "refused", "error", "timeout", "cancelled"):
                        terminal = event.get("payload") or {}
                        break
                else:
                    obs["client_reason"] = "stream_ended_without_terminal"
    except httpx.TimeoutException as exc:
        # The client gave up waiting; closing the connection cancels the server pipeline.
        obs["http_error"] = type(exc).__name__
        obs["client_reason"] = "client_abandoned"
    except httpx.HTTPError as exc:
        obs["http_error"] = type(exc).__name__
        obs["client_reason"] = f"client_transport_error:{type(exc).__name__}"
    obs["total_ms"] = (time.perf_counter() - t0) * 1000
    return obs, terminal


def _jsonl(path: Path) -> list[dict[str, Any]]:
    items = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                continue  # a partially written last line; the server owns these files
    return items


def _server_record(run_dir: Path, request_id: str) -> dict[str, Any] | None:
    record = None
    for item in _jsonl(run_dir / "requests.jsonl"):
        if item.get("request_id") == request_id:
            record = item
    if record is not None:
        record["calls"] = [
            c for c in _jsonl(run_dir / "calls.jsonl") if c.get("request_id") == request_id
        ]
    return record


async def _await_server_record(run_dir: Path, request_id: str,
                               wait_s: float) -> dict[str, Any] | None:
    """After an abandoned request the server writes its record only once cancellation
    completes, so poll briefly instead of racing it."""
    deadline = time.perf_counter() + wait_s
    while True:
        record = _server_record(run_dir, request_id)
        if record is not None or time.perf_counter() >= deadline:
            return record
        await asyncio.sleep(0.1)


def append_results(path: Path, rows: list[dict[str, Any]]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerows(rows)


async def run(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None = None) -> list[dict[str, Any]]:
    questions = list(args.question or [])
    if args.questions_file:
        questions += [q for q in Path(args.questions_file).read_text(encoding="utf-8").splitlines()
                      if q.strip()]
    if len(questions) > args.max_requests:
        raise SystemExit(f"{len(questions)} questions exceed --max-requests {args.max_requests}")
    rows = []
    timeout = httpx.Timeout(args.client_timeout_s)
    async with httpx.AsyncClient(base_url=args.api, timeout=timeout, transport=transport) as client:
        meta = (await client.get("/config")).json()
        run_dir = Path(meta["evidence_dir"]) if meta.get("evidence_dir") else Path(args.out_dir)
        print(f"source={meta['source']} model={meta['model']} run_id={meta['run_id']}")
        if meta["source"] != "real":
            print("NOTE: offline test double -- not model output, not usable as real evidence.")
        for n, question in enumerate(questions, start=1):
            if n > 1 and args.sleep_between_s > 0:
                await asyncio.sleep(args.sleep_between_s)
            obs, terminal = await ask_once(client, question, args.retrieval_setting,
                                           args.scenario, f"{args.question_id_prefix}{n:02d}")
            server = await _await_server_record(
                run_dir, obs["request_id"],
                0.0 if terminal else getattr(args, "server_record_wait_s", 3.0))
            row = _row(obs, terminal, meta, server)
            rows.append(row)
            obs["terminal"] = terminal
            with open(run_dir / "client_observations.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obs) + "\n")
            # Written per request so an interrupted run keeps every completed row.
            append_results(run_dir / "results.csv", [row])
            print(f"[{row['question_id']}] status={row['status']} "
                  f"reason={row['outcome_reason'] or '-'} first_token_ms={row['first_token_ms'] or '-'} "
                  f"total_ms={row['total_ms']} tokens_in={row['tokens_in']} tokens_out={row['tokens_out']}")
            if terminal and terminal.get("answer"):
                print("  answer (post-verification):", terminal["answer"])
            elif terminal:
                print("  message:", terminal.get("message"))
        print(f"results.csv -> {run_dir / 'results.csv'}")
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="PaperRelay client smoke runner")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--question", action="append")
    parser.add_argument("--questions-file")
    parser.add_argument("--retrieval-setting", default="default")
    parser.add_argument("--scenario", default="smoke")
    parser.add_argument("--question-id-prefix", default="smoke-")
    parser.add_argument("--max-requests", type=int, default=2)
    parser.add_argument("--sleep-between-s", type=float, default=0.0)
    parser.add_argument("--client-timeout-s", type=float, default=120.0)
    parser.add_argument("--server-record-wait-s", type=float, default=3.0)
    parser.add_argument("--out-dir", default="evidence/client-only")
    args = parser.parse_args(argv)
    if not (args.question or args.questions_file):
        parser.error("give --question or --questions-file")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
