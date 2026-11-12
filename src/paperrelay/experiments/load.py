"""Open-loop load runner: fire each scheduled request at its time, regardless of others.

    python -m paperrelay.experiments.load --scenario experiments/scenarios/steady.json \
        --api http://127.0.0.1:8000 [--standin-admin-url http://127.0.0.1:8100]

Normally launched by `python -m paperrelay.experiments.run`, which also starts the API and
stand-in processes. Every scheduled request yields exactly one results.csv row: delivered,
refused, rate-limited, errored, abandoned by the client (status timeout, reason
client_abandoned) and never sent (status error, reason not_sent:<why>, blank send time).

Rows are appended to results.csv (and client_observations.jsonl) as each request reaches a
terminal state; at completion the file is verified (one row per scheduled request) and
atomically rewritten in schedule order with the server-record join. results_status.json
and the run_end event carry run_status: completed, interrupted (cancelled) or failed; a
results_status.json still saying in_progress means the runner was killed. Only
`completed` means results.csv is complete.

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
RESULTS_STATUS = "results_status.json"


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


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    write_csv(tmp, rows)
    os.replace(tmp, path)


def _write_status(run_dir: Path, record: dict[str, Any]) -> None:
    tmp = run_dir / (RESULTS_STATUS + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, run_dir / RESULTS_STATUS)


class IncrementalResults:
    """results.csv and client_observations.jsonl, appended one record per request as soon
    as it is terminal. Each append opens, writes and closes (flushing to the OS), so a
    killed or cancelled runner keeps every finished row. No fsync: that would block the
    event loop that schedules arrivals; the guarantee covers process death, not OS crash."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.csv_path = run_dir / "results.csv"
        self.obs_path = run_dir / "client_observations.jsonl"
        self.row_ids: list[str] = []
        self.obs_ids: set[str] = set()
        with open(self.csv_path, "x", newline="", encoding="utf-8") as fh:  # never overwrite
            csv.DictWriter(fh, fieldnames=COLUMNS).writeheader()

    def add_row(self, row: dict[str, Any]) -> None:
        with open(self.csv_path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=COLUMNS).writerow(row)
        self.row_ids.append(str(row["request_id"]))

    def add_obs(self, request_id: str, record: dict[str, Any]) -> None:
        _append_jsonl(self.obs_path, record)
        self.obs_ids.add(request_id)

    def verify_complete(self, expected: list[str]) -> None:
        """The incremental file (read back from disk) has exactly one row per scheduled
        request before it is replaced by the schedule-ordered final file."""
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            on_disk = [r["request_id"] for r in csv.DictReader(fh)]
        if sorted(on_disk) != sorted(expected) or len(set(on_disk)) != len(on_disk):
            raise LoadError(f"incremental results.csv has {len(on_disk)} rows "
                            f"({len(set(on_disk))} distinct ids) for {len(expected)} "
                            f"scheduled requests")


@dataclass
class _Progress:
    """What the interruption handler needs, filled in as the run advances."""
    planned: int = 0
    t0: float = 0.0
    revision_start: str = ""
    writer: IncrementalResults | None = None
    events_path: Path | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


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
    """Run the scenario. If this coroutine is cancelled (e.g. by the orchestrator's
    wall-clock limit) or fails after results.csv was created, results.csv keeps every row
    already written, and run_end / results_status.json record run_status interrupted or
    failed: the file is then partial (no rows are invented for unfinished requests)."""
    prog = _Progress()
    try:
        return await _run_load(scenario, prog, api=api, out_dir=out_dir,
                               standin_url=standin_url, transport=transport,
                               standin_transport=standin_transport,
                               run_meta_extra=run_meta_extra)
    except BaseException as exc:
        await _record_interruption(prog, exc)
        raise


async def _cancel_all(tasks: list[asyncio.Task[Any]]) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _record_interruption(prog: _Progress, exc: BaseException) -> None:
    await _cancel_all([t for t in prog.tasks if not t.done()])
    if prog.writer is None or prog.events_path is None:
        return  # failed before results.csv was created: nothing partial to label
    run_status = ("interrupted" if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt)
                  else "failed")
    rec: dict[str, Any] = {
        "kind": "run_end", "run_status": run_status, "error": repr(exc)[:300],
        "results_csv": "partial", "rows_written": len(prog.writer.row_ids),
        "scheduled": prog.planned,
        "note": "results.csv holds one row per request that reached a terminal state before "
                "the run stopped (client-side values, no server-record join); unfinished "
                "and unsent requests have no row",
        **_revisions(prog.revision_start, git_revision())}
    rec = {"offset_s": round(time.perf_counter() - prog.t0, 4), "utc": utc_now(), **rec}
    _append_jsonl(prog.events_path, rec)
    _write_status(prog.writer.run_dir, {k: v for k, v in rec.items() if k != "kind"})


def _revisions(start: str, end: str) -> dict[str, Any]:
    return {"revision_start": start, "revision_end": end,
            "revision_changed_during_run": start != end}


async def _run_load(scenario: Scenario, prog: _Progress, *, api: str, out_dir: Path | None,
                    standin_url: str | None, transport: httpx.AsyncBaseTransport | None,
                    standin_transport: httpx.AsyncBaseTransport | None,
                    run_meta_extra: dict[str, Any] | None) -> LoadResult:
    questions = scenario.questions()
    planned = plan(scenario, questions)
    prog.planned = len(planned)
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
        prog.revision_start = git_revision()

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
        prog.t0 = t0
        scenario_meta = {
            "scenario": scenario.raw, "scenario_path": scenario.path,
            "scenario_hash": scenario.scenario_hash, "seed": scenario.seed,
            "arrival": scenario.arrival, "requests": scenario.requests,
            "window_s": scenario.window_s, "lag_threshold_ms": scenario.lag_threshold_ms,
            "client_timeout_s": scenario.client_timeout_s,
            "calibration_file": file_id(scenario.calibration_file),
            "questions_file": file_id(scenario.questions_file),
            "runner_commit": prog.revision_start, "revision_start": prog.revision_start,
            "revision_end_note": "revision_end and revision_changed_during_run are recorded "
                                 f"in the run_end event and {RESULTS_STATUS}",
            "results_status_file": RESULTS_STATUS,
            "server_config": meta,
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
        _write_status(run_dir, {
            "run_status": "in_progress", "results_csv": "incremental",
            "scheduled": len(planned), "revision_start": prog.revision_start,
            "note": "rows are appended as requests finish; this status is replaced when the "
                    "run completes or is interrupted. If it still says in_progress, the "
                    "runner was killed and results.csv is partial."})
        writer = IncrementalResults(run_dir)
        prog.writer = writer
        events_path = run_dir / "run_events.jsonl"
        run_events: list[dict[str, Any]] = []

        def event(rec: dict[str, Any]) -> None:
            rec = {"offset_s": round(time.perf_counter() - t0, 4), "utc": utc_now(), **rec}
            run_events.append(rec)
            _append_jsonl(events_path, rec)

        event({"kind": "run_start", "run_id": run_id, "scenario": scenario.name})
        prog.events_path = events_path
        state: dict[int, dict[str, Any]] = {}
        results: dict[int, tuple[dict[str, Any], dict[str, Any] | None]] = {}

        def extra_for(p: Planned) -> dict[str, Any]:
            extra = {**extra_base, "scheduled_start_time":
                     (t0_utc + timedelta(seconds=p.offset_s)).isoformat()}
            st = state.get(p.index)
            if st is not None:
                extra["arrival_lag_ms"] = round(st["lag_ms"], 1)
            return extra

        def obs_record(p: Planned, obs: dict[str, Any],
                       terminal: dict[str, Any] | None) -> dict[str, Any]:
            lag = state[p.index]["lag_ms"]
            return {**obs, "scenario": scenario.name, "question_id": p.question_id,
                    "scheduled_offset_s": p.offset_s, "arrival_lag_ms": lag,
                    "scheduling_failure": lag > scenario.lag_threshold_ms,
                    "terminal": terminal}

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
            # On disk as soon as the request is terminal. The server-record join (for
            # requests without a client-side terminal payload) happens at completion, when
            # results.csv is rewritten in schedule order.
            writer.add_row(build_row(obs, terminal, meta, None, scenario=scenario.name,
                                     question_id=p.question_id, extra=extra_for(p)))
            writer.add_obs(p.request_id, obs_record(p, obs, terminal))

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
        prog.tasks = [*tasks, *controls, poller]
        hard_stop = scenario.max_runtime_s - scenario.server_record_wait_s
        try:
            done, pending = await asyncio.wait(tasks + controls,
                                               timeout=max(1.0, hard_stop))
        except BaseException:
            # Cancel requests while the HTTP client is still open, so closing the client
            # cannot turn in-flight requests into transport-error rows.
            await _cancel_all(prog.tasks)
            raise
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
    inconsistent: list[str] = []
    written = set(writer.row_ids)
    for p in planned:
        extra = extra_for(p)
        st = state.get(p.index)
        if st is None:
            row = not_sent_row(
                request_id=p.request_id, scenario=scenario.name, question_id=p.question_id,
                retrieval_setting=scenario.retrieval_setting,
                reason="not_sent:runner_runtime_cap" if cap_hit else "not_sent:task_failed",
                meta=meta, extra=extra)
        else:
            if p.index in results:
                obs, terminal = results[p.index]
            else:
                # Sent but still running when the runner cap fired (or the task crashed).
                obs = {"request_id": p.request_id,
                       "retrieval_setting": scenario.retrieval_setting,
                       "start_time": (t0_utc + timedelta(seconds=st["send_perf"] - t0)
                                      ).isoformat(),
                       "first_token_ms": None, "events": [],
                       "total_ms": (cap_offset - (st["send_perf"] - t0)) * 1000,
                       "client_reason": "runner_runtime_cap" if cap_hit
                       else "client_task_failed"}
                terminal = None
            diag: dict[str, Any] = {}
            row = build_row(obs, terminal, meta, records.get(p.request_id),
                            scenario=scenario.name, question_id=p.question_id, extra=extra,
                            diagnostics=diag)
            if obs.get("client_reason") == "runner_runtime_cap":
                row["status"], row["outcome_reason"] = "timeout", "runner_runtime_cap"
            if diag.get("partition_inconsistent"):
                inconsistent.append(p.request_id)
                event({"kind": "partition_inconsistent", "request_id": p.request_id,
                       "status": row["status"], "outcome_reason": row["outcome_reason"],
                       **diag, "working_ms_in_results": "blank (not derivable)"})
            if p.request_id not in writer.obs_ids:
                writer.add_obs(p.request_id, {
                    **obs_record(p, obs, terminal),
                    "partition_inconsistent": bool(diag.get("partition_inconsistent"))})
        rows.append(row)
        if p.request_id not in written:
            writer.add_row(row)  # unsent / cap-hit / crashed: on disk before the rewrite
    if len(rows) != len(planned):
        raise LoadError("internal: row count differs from scheduled count")
    writer.verify_complete([p.request_id for p in planned])
    write_csv_atomic(run_dir / "results.csv", rows)
    missing = sorted(sent_ids - set(records)) if server_dir is not None else []
    end = {"kind": "run_end", "run_status": "completed", "results_csv": "complete",
           "rows": len(rows), "sent": len(sent_ids),
           "not_sent": len(planned) - len(sent_ids), "runtime_cap_hit": cap_hit,
           "server_records_missing": missing, "capacity_at_end": capacity_end,
           "partition_inconsistent": inconsistent,
           **_revisions(prog.revision_start, git_revision())}
    end = {"offset_s": round(time.perf_counter() - t0, 4), "utc": utc_now(), **end}
    run_events.append(end)
    _append_jsonl(events_path, end)
    _write_status(run_dir, {k: v for k, v in end.items()
                            if k not in ("kind", "capacity_at_end")})
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
