"""Open-loop load runner: fire each scheduled request at its time, regardless of others.

    python -m paperrelay.experiments.load --scenario experiments/scenarios/steady.json \
        --api http://127.0.0.1:8000 [--standin-admin-url http://127.0.0.1:8100]

Normally launched by `python -m paperrelay.experiments.run`, which also starts the API and
stand-in processes. Every scheduled request yields exactly one results.csv row: delivered,
refused, rate-limited, errored, abandoned by the client (status timeout, reason
client_abandoned) and never sent (status error, reason not_sent:<why>, blank send time).

Clocks: scheduling and client durations use time.perf_counter(); wall-clock UTC is
recorded for start times. arrival_lag_ms = actual send - scheduled send; a lag above the
scenario's lag_threshold_ms is a scheduling failure (recorded in client_observations.jsonl
and run_events.jsonl, and counted by the analysis).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from paperrelay.client.smoke import COLUMNS
from paperrelay.config import git_revision
from paperrelay.experiments.client import (
    await_server_records,
    build_row,
    not_sent_row,
    stream_ask,
    utc_now,
)
from paperrelay.experiments.scenario import (
    ControlAction,
    Scenario,
    arrival_offsets,
    load_scenario,
    question_for,
    request_id_for,
    resolve,
)

SCHEDULE_LEAD_S = 0.5


class LoadError(RuntimeError):
    pass


@dataclass
class Planned:
    index: int
    request_id: str
    question_id: str
    question: str
    offset_s: float


@dataclass
class LoadResult:
    rows: list[dict[str, Any]]
    run_dir: Path
    run_events: list[dict[str, Any]] = field(default_factory=list)


def plan(scenario: Scenario, questions: list[str]) -> list[Planned]:
    offsets = arrival_offsets(scenario.requests, scenario.window_s, scenario.arrival,
                              scenario.seed)
    out = []
    for i, off in enumerate(offsets):
        qid, q = question_for(i, questions, scenario.seed)
        out.append(Planned(i, request_id_for(scenario.seed, i), qid, q, off))
    return out


def file_id(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = resolve(path)
    if not p.exists():
        return {"path": path, "sha256": None, "note": "file not present when the run started"}
    return {"path": path, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


def host_info() -> dict[str, Any]:
    # No hostname/user name: those are private identifiers.
    return {"platform": platform.platform(), "machine": platform.machine(),
            "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "implementation": platform.python_implementation()}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


async def _post_json(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"url_path": httpx.URL(url).path, "body": body,
                           "client_utc_sent": utc_now()}
    t = time.perf_counter()
    try:
        resp = await client.post(url, json=body)
        out["http_status"] = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = {"non_json": resp.text[:200]}
        out["response"] = data
        out["applied_utc"] = data.get("applied_utc") if isinstance(data, dict) else None
    except httpx.HTTPError as exc:
        out["error"] = type(exc).__name__
        out["applied_utc"] = None
    out["client_utc_done"] = utc_now()
    out["client_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return out


async def apply_control(api: httpx.AsyncClient, standin: httpx.AsyncClient | None,
                        standin_url: str | None, action: ControlAction) -> dict[str, Any]:
    """API /admin/limits then stand-in /admin/config, back to back; both applied_utc kept."""
    rec: dict[str, Any] = {"kind": "control_action", "name": action.name,
                           "scheduled_offset_s": action.at_s}
    if action.api_limits:
        rec["api"] = await _post_json(api, "/admin/limits", action.api_limits)
    if action.standin_config:
        if standin is None or not standin_url:
            rec["standin"] = {"error": "no stand-in admin URL configured", "applied_utc": None}
        else:
            rec["standin"] = await _post_json(
                standin, standin_url.rstrip("/") + "/admin/config", action.standin_config)
    parts = [rec.get("api"), rec.get("standin")]
    rec["all_applied"] = all(p is not None and p.get("applied_utc") for p in parts
                             if p is not None)
    return rec


async def run_load(scenario: Scenario, *, api: str, out_dir: Path | None = None,
                   standin_url: str | None = None,
                   transport: httpx.AsyncBaseTransport | None = None,
                   standin_transport: httpx.AsyncBaseTransport | None = None,
                   run_meta_extra: dict[str, Any] | None = None) -> LoadResult:
    questions = scenario.questions()
    planned = plan(scenario, questions)
    limits = httpx.Limits(max_connections=scenario.requests + 8,
                          max_keepalive_connections=scenario.requests + 8)
    # Per-operation backstop only; abandonment is the whole-request deadline in stream_ask.
    timeout = httpx.Timeout(scenario.client_timeout_s + 5.0)
    async with httpx.AsyncClient(base_url=api, transport=transport, timeout=timeout,
                                 limits=limits) as client, \
            httpx.AsyncClient(transport=standin_transport, timeout=10.0) as admin:
        cfg_resp = await client.get("/config")
        cfg_resp.raise_for_status()
        meta: dict[str, Any] = cfg_resp.json()
        if meta.get("source") != scenario.source:
            raise LoadError(f"API source {meta.get('source')!r} != scenario source "
                            f"{scenario.source!r}; refusing to mix sources")
        if meta.get("source") == "real":
            raise LoadError("the load runner never targets the real provider")
        server_dir = Path(meta["evidence_dir"]) if meta.get("evidence_dir") else None
        run_dir = out_dir or server_dir
        if run_dir is None:
            raise LoadError("no evidence_dir from /config and no --out-dir")
        run_dir.mkdir(parents=True, exist_ok=True)
        if (run_dir / "results.csv").exists():
            raise LoadError(f"{run_dir / 'results.csv'} exists; refusing to overwrite")
        run_id = str(meta.get("run_id") or run_dir.name)
        server_meta: dict[str, Any] = {}
        if (run_dir / "run_meta.json").exists():
            server_meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
        capacity_start = await _get_json(client, "/capacity")

        extra_base = {
            "run_id": run_id, "seed": scenario.seed, "offered_window_s": scenario.window_s,
            "pipeline_commit": server_meta.get("pipeline_commit", ""),
            "config_hash": server_meta.get("config_hash", ""),
            "cost_basis": "not_measured",
        }
        # Schedule origin a fixed lead in the future, so setup (meta files, task creation)
        # is not charged to the first arrivals' lag.
        t0 = time.perf_counter() + SCHEDULE_LEAD_S
        t0_utc = datetime.now(UTC) + timedelta(seconds=SCHEDULE_LEAD_S)
        scenario_meta = {
            "scenario": scenario.raw, "scenario_path": scenario.path,
            "scenario_hash": scenario.scenario_hash, "seed": scenario.seed,
            "arrival": scenario.arrival, "requests": scenario.requests,
            "window_s": scenario.window_s, "lag_threshold_ms": scenario.lag_threshold_ms,
            "client_timeout_s": scenario.client_timeout_s,
            "calibration_file": file_id(scenario.calibration_file),
            "questions_file": file_id(scenario.questions_file),
            "runner_commit": git_revision(), "server_config": meta,
            "server_run_meta": {k: server_meta.get(k) for k in
                                ("pipeline_commit", "config_hash", "source", "model",
                                 "started_utc")},
            "capacity_at_start": capacity_start, "host": host_info(),
            "run_start_utc": t0_utc.isoformat(),
            "schedule": [{"index": p.index, "request_id": p.request_id,
                          "question_id": p.question_id, "offset_s": round(p.offset_s, 6)}
                         for p in planned],
            **(run_meta_extra or {}),
        }
        (run_dir / "scenario_meta.json").write_text(json.dumps(scenario_meta, indent=2),
                                                    encoding="utf-8")
        events_path = run_dir / "run_events.jsonl"
        run_events: list[dict[str, Any]] = []

        def event(rec: dict[str, Any]) -> None:
            rec = {"offset_s": round(time.perf_counter() - t0, 4), "utc": utc_now(), **rec}
            run_events.append(rec)
            _append_jsonl(events_path, rec)

        event({"kind": "run_start", "run_id": run_id, "scenario": scenario.name})
        state: dict[int, dict[str, Any]] = {}
        results: dict[int, tuple[dict[str, Any], dict[str, Any] | None]] = {}

        async def fire(p: Planned) -> None:
            scheduled = t0 + p.offset_s
            await sleep_until(scheduled)
            send = time.perf_counter()
            lag_ms = (send - scheduled) * 1000
            state[p.index] = {"send_perf": send, "lag_ms": lag_ms}
            if lag_ms > scenario.lag_threshold_ms:
                event({"kind": "scheduling_failure", "request_id": p.request_id,
                       "arrival_lag_ms": round(lag_ms, 1),
                       "threshold_ms": scenario.lag_threshold_ms})
            obs, terminal = await stream_ask(
                client, request_id=p.request_id, question=p.question,
                retrieval_setting=scenario.retrieval_setting,
                timeout_s=scenario.client_timeout_s, t_ref=t0)
            results[p.index] = (obs, terminal)

        async def control(action: ControlAction) -> None:
            await sleep_until(t0 + action.at_s)
            started = round(time.perf_counter() - t0, 4)
            event({**await apply_control(client, admin, standin_url, action),
                   "actual_start_offset_s": started})

        async def poll_capacity() -> None:
            if scenario.capacity_poll_s <= 0:
                return
            while True:
                data = await _get_json(client, "/capacity")
                if data is None or "error" in data:
                    event({"kind": "capacity_unavailable", "detail": data})
                    return
                event({"kind": "capacity_sample", "capacity": data})
                await asyncio.sleep(scenario.capacity_poll_s)

        tasks = [asyncio.create_task(fire(p)) for p in planned]
        controls = [asyncio.create_task(control(a)) for a in scenario.control_actions]
        poller = asyncio.create_task(poll_capacity())
        hard_stop = scenario.max_runtime_s - scenario.server_record_wait_s
        done, pending = await asyncio.wait(tasks + controls,
                                           timeout=max(1.0, hard_stop))
        cap_hit = bool(pending)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        for t in done:
            if t.exception() is not None:
                event({"kind": "task_exception", "error": repr(t.exception())[:300]})
        if cap_hit:
            event({"kind": "runner_runtime_cap", "pending_tasks": len(pending)})
        cap_offset = time.perf_counter() - t0
        capacity_end = await _get_json(client, "/capacity")

    sent_ids = {p.request_id for p in planned if p.index in state}
    records = {}
    if server_dir is not None and server_dir.exists():
        records = await await_server_records(server_dir, sent_ids, scenario.server_record_wait_s)
    rows: list[dict[str, Any]] = []
    for p in planned:
        extra = {**extra_base,
                 "scheduled_start_time": (t0_utc + timedelta(seconds=p.offset_s)).isoformat()}
        st = state.get(p.index)
        if st is None:
            rows.append(not_sent_row(
                request_id=p.request_id, scenario=scenario.name, question_id=p.question_id,
                retrieval_setting=scenario.retrieval_setting,
                reason="not_sent:runner_runtime_cap" if cap_hit else "not_sent:task_failed",
                meta=meta, extra=extra))
            continue
        extra["arrival_lag_ms"] = round(st["lag_ms"], 1)
        if p.index in results:
            obs, terminal = results[p.index]
        else:
            # Sent but still running when the runner cap fired (or the task crashed).
            obs = {"request_id": p.request_id, "retrieval_setting": scenario.retrieval_setting,
                   "start_time": (t0_utc + timedelta(seconds=st["send_perf"] - t0)).isoformat(),
                   "first_token_ms": None, "events": [],
                   "total_ms": (cap_offset - (st["send_perf"] - t0)) * 1000,
                   "client_reason": "runner_runtime_cap" if cap_hit else "client_task_failed"}
            terminal = None
        row = build_row(obs, terminal, meta, records.get(p.request_id),
                        scenario=scenario.name, question_id=p.question_id, extra=extra)
        if obs.get("client_reason") == "runner_runtime_cap":
            row["status"], row["outcome_reason"] = "timeout", "runner_runtime_cap"
        rows.append(row)
        _append_jsonl(run_dir / "client_observations.jsonl", {
            **obs, "scenario": scenario.name, "question_id": p.question_id,
            "scheduled_offset_s": p.offset_s, "arrival_lag_ms": st["lag_ms"],
            "scheduling_failure": st["lag_ms"] > scenario.lag_threshold_ms,
            "terminal": terminal})
    write_csv(run_dir / "results.csv", rows)
    missing = sorted(sent_ids - set(records)) if server_dir is not None else []
    end = {"kind": "run_end", "rows": len(rows), "sent": len(sent_ids),
           "not_sent": len(planned) - len(sent_ids), "runtime_cap_hit": cap_hit,
           "server_records_missing": missing, "capacity_at_end": capacity_end}
    end = {"offset_s": round(time.perf_counter() - t0, 4), "utc": utc_now(), **end}
    run_events.append(end)
    _append_jsonl(events_path, end)
    if len(rows) != len(planned):
        raise LoadError("internal: row count differs from scheduled count")
    return LoadResult(rows=rows, run_dir=run_dir, run_events=run_events)


async def sleep_until(target_perf: float) -> None:
    """Sleep until time.perf_counter() >= target. Event-loop timers can wake a few ms
    early (coarse Windows timer), so re-sleep for the remainder instead of firing early."""
    while (remaining := target_perf - time.perf_counter()) > 0:
        await asyncio.sleep(remaining)


async def _get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(path)
    except httpx.HTTPError as exc:
        return {"error": type(exc).__name__}
    if resp.status_code != 200:
        return {"error": f"http_{resp.status_code}"}
    try:
        data = resp.json()
    except ValueError:
        return {"error": "non_json"}
    return data if isinstance(data, dict) else {"error": "not_an_object"}


def summarize(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="PaperRelay open-loop load runner")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--standin-admin-url", default=None,
                        help="stand-in base URL for /admin/config, e.g. http://127.0.0.1:8100")
    parser.add_argument("--out-dir", default=None,
                        help="override run dir (default: the API's evidence_dir)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    scenario = load_scenario(Path(args.scenario))
    result = asyncio.run(run_load(scenario, api=args.api,
                                  out_dir=Path(args.out_dir) if args.out_dir else None,
                                  standin_url=args.standin_admin_url))
    print(f"rows={len(result.rows)} {summarize(result.rows)} -> {result.run_dir}")


if __name__ == "__main__":
    main()
