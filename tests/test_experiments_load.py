"""Open-loop load runner against a fake in-process API (no stand-in, no provider)."""

from __future__ import annotations

import asyncio
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from paperrelay.client.smoke import COLUMNS
from paperrelay.experiments import load as load_mod
from paperrelay.experiments.client import not_sent_row
from paperrelay.experiments.scenario import (
    ScenarioError,
    arrival_offsets,
    parse_scenario,
    request_id_for,
)

FX = Path(__file__).parent / "fixtures" / "experiments"


def scenario_raw(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "t", "source": "stand-in", "seed": 42, "requests": 4, "window_s": 0.4,
        "arrival": "even", "questions_file": str(FX / "workload_questions.txt"),
        "client_timeout_s": 2.0, "lag_threshold_ms": 50.0, "max_requests": 10,
        "max_runtime_s": 20.0, "server_record_wait_s": 0.2, "capacity_poll_s": 0,
    }
    raw.update(over)
    return raw


def done_payload(rid: str) -> dict[str, Any]:
    return {"request_id": rid, "status": "answered", "source": "stand-in", "model": "fake",
            "retrieval_setting": "medium", "provider_calls": 3, "unknown_usage_calls": 0,
            "tokens_in": 10, "tokens_out": 2, "retries": 1, "queue_wait_ms": 5.0,
            "rate_wait_ms": 3.0, "retry_wait_ms": 7.0, "queue_peak_observed": 2,
            "server_total_ms": 20.0}


def fake_api(run_dir: Path, mode: str = "ok", source: str = "stand-in") -> tuple[FastAPI, dict]:
    app = FastAPI()
    state: dict[str, Any] = {"arrivals": [], "admin": []}

    @app.get("/config")
    async def config() -> dict[str, Any]:
        return {"source": source, "model": "fake", "run_id": "fake-run",
                "evidence_dir": str(run_dir), "retrieval_settings": ["medium"]}

    @app.get("/capacity")
    async def capacity() -> dict[str, Any]:
        return {"queue_depth": 0, "in_flight": 0}

    @app.post("/admin/limits")
    async def admin(request: Request) -> dict[str, Any]:
        body = await request.json()
        state["admin"].append(body)
        return {"applied_utc": datetime.now().isoformat(), "limits": body}

    @app.post("/ask")
    async def ask(request: Request) -> Any:
        body = await request.json()
        state["arrivals"].append((time.perf_counter(), body["request_id"]))
        if mode == "block" and len(state["arrivals"]) == 1:
            time.sleep(0.35)  # blocks the shared event loop: later sends must lag
        if mode == "http429":
            return JSONResponse({"detail": "queue full"}, status_code=429)

        async def gen():
            yield json.dumps({"kind": "received", "ts_ms": 0, "payload": {}}) + "\n"
            if mode == "slow":
                await asyncio.sleep(5)
            yield json.dumps({"kind": "answer_token", "ts_ms": 1,
                              "payload": {"text": "x"}}) + "\n"
            yield json.dumps({"kind": "done", "ts_ms": 2,
                              "payload": done_payload(body["request_id"])}) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    return app, state


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def test_schedule_is_deterministic_and_inside_window():
    for arrival in ("poisson", "uniform", "even"):
        a = arrival_offsets(30, 5.0, arrival, seed=7)
        b = arrival_offsets(30, 5.0, arrival, seed=7)
        assert a == b and len(a) == 30
        assert all(0 <= x < 5.0 for x in a) and a == sorted(a)
    assert arrival_offsets(30, 5.0, "poisson", 7) != arrival_offsets(30, 5.0, "poisson", 8)
    assert request_id_for(7, 3) == request_id_for(7, 3) != request_id_for(8, 3)
    sc = parse_scenario(scenario_raw())
    p1 = load_mod.plan(sc, sc.questions())
    p2 = load_mod.plan(sc, sc.questions())
    assert [(p.request_id, p.question_id, p.offset_s) for p in p1] == \
        [(p.request_id, p.question_id, p.offset_s) for p in p2]


@pytest.mark.parametrize("bad", [
    {"window_s": 0}, {"requests": 0}, {"requests": 11},
    {"api_env": {"PAPERRELAY_PROVIDER": "groq"}},
    {"arrival": "closed_loop"},
    {"max_runtime_s": 1.0},
    {"control_actions": [{"at_s": 5.0, "api_limits": {"rpm": 1}}]},
])
def test_scenario_validation_rejects(bad):
    with pytest.raises(ScenarioError):
        parse_scenario(scenario_raw(**bad))


async def test_open_loop_rows_one_per_scheduled_request(tmp_path):
    app, state = fake_api(tmp_path)
    sc = parse_scenario(scenario_raw())
    result = await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    header, rows = read_rows(tmp_path / "results.csv")
    assert header == COLUMNS
    planned = load_mod.plan(sc, sc.questions())
    assert [r["request_id"] for r in rows] == [p.request_id for p in planned]
    assert len(result.rows) == 4 and {r["status"] for r in rows} == {"ok"}
    events = [json.loads(x) for x in (tmp_path / "run_events.jsonl").read_text().splitlines()]
    flagged = {e["request_id"] for e in events if e["kind"] == "partition_inconsistent"}
    for r in rows:
        assert float(r["arrival_lag_ms"]) >= 0
        assert r["offered_window_s"] == "0.4" and r["seed"] == "42"
        assert r["retries"] == "1" and r["queue_peak_observed"] == "2"
        assert float(r["waiting_ms"]) == 8.0 and float(r["retry_wait_ms"]) == 7.0
        # The fake reports 15 ms of server waits; an in-process request can finish faster
        # than that, in which case the partition is impossible: blank + flagged, never < 0.
        if r["working_ms"] == "":
            assert float(r["total_ms"]) <= 15.05 and r["request_id"] in flagged
        else:
            assert float(r["working_ms"]) == pytest.approx(float(r["total_ms"]) - 15.0,
                                                           abs=0.11)
            assert r["request_id"] not in flagged
        assert r["first_token_ms"] != "" and r["tokens_in"] == "10"
    sched = [datetime.fromisoformat(r["scheduled_start_time"]) for r in rows]
    gaps = [(b - a).total_seconds() for a, b in zip(sched, sched[1:], strict=False)]
    assert gaps == pytest.approx([0.1, 0.1, 0.1], abs=1e-6)
    meta = json.loads((tmp_path / "scenario_meta.json").read_text())
    assert meta["seed"] == 42 and meta["window_s"] == 0.4 and len(meta["schedule"]) == 4
    assert "hostname" not in json.dumps(meta["host"])
    kinds = [json.loads(x)["kind"] for x in (tmp_path / "run_events.jsonl").read_text().splitlines()]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"


async def test_lag_is_recorded_when_the_sender_falls_behind(tmp_path):
    app, state = fake_api(tmp_path, mode="block")
    sc = parse_scenario(scenario_raw())
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    _, rows = read_rows(tmp_path / "results.csv")
    lags = [float(r["arrival_lag_ms"]) for r in rows]
    assert len(state["arrivals"]) == 4  # late requests are still sent (open loop)
    assert max(lags[1:]) > 50.0
    events = [json.loads(x) for x in (tmp_path / "run_events.jsonl").read_text().splitlines()]
    failures = [e for e in events if e["kind"] == "scheduling_failure"]
    assert failures and all(e["arrival_lag_ms"] > 50.0 for e in failures)
    obs = [json.loads(x) for x in
           (tmp_path / "client_observations.jsonl").read_text().splitlines()]
    assert sum(o["scheduling_failure"] for o in obs) == len(failures)


async def test_client_deadline_is_abandonment(tmp_path):
    app, _ = fake_api(tmp_path, mode="slow")
    sc = parse_scenario(scenario_raw(requests=2, client_timeout_s=0.3))
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    _, rows = read_rows(tmp_path / "results.csv")
    assert len(rows) == 2
    for r in rows:
        assert r["status"] == "timeout" and r["outcome_reason"] == "client_abandoned"
        assert r["first_token_ms"] == ""  # blank, never 0
        assert 250 <= float(r["total_ms"]) < 2000


async def test_http_429_admission_is_rate_limited_with_zero_known_tokens(tmp_path):
    app, _ = fake_api(tmp_path, mode="http429")
    sc = parse_scenario(scenario_raw(requests=1))
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    _, [row] = read_rows(tmp_path / "results.csv")
    assert row["status"] == "rate_limited" and row["outcome_reason"] == "http_429"
    assert row["tokens_in"] == "0" and row["tokens_usage_complete"] == "True"


async def test_control_action_hits_api_and_standin_and_records_both(tmp_path):
    app, state = fake_api(tmp_path)

    def standin(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/config"
        return httpx.Response(200, json={"applied_utc": "2026-11-12T00:00:01+00:00",
                                         "config": json.loads(request.content)})

    sc = parse_scenario(scenario_raw(control_actions=[
        {"at_s": 0.2, "name": "halve", "api_limits": {"rpm": 12, "tpm": 3000},
         "standin_config": {"limits": {"rpm": 15, "tpm": 4000}}}]))
    await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app),
                            standin_url="http://standin", standin_transport=httpx.MockTransport(
                                standin))
    events = [json.loads(x) for x in (tmp_path / "run_events.jsonl").read_text().splitlines()]
    [act] = [e for e in events if e["kind"] == "control_action"]
    assert act["api"]["applied_utc"] and act["standin"]["applied_utc"]
    assert act["all_applied"] is True and act["actual_start_offset_s"] >= 0.2
    assert state["admin"] == [{"rpm": 12, "tpm": 3000}]


async def test_refuses_source_mismatch_and_existing_results(tmp_path):
    app, _ = fake_api(tmp_path, source="real")
    sc = parse_scenario(scenario_raw())
    with pytest.raises(load_mod.LoadError, match="source"):
        await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app))
    app2, _ = fake_api(tmp_path)
    (tmp_path / "results.csv").write_text("x\n")
    with pytest.raises(load_mod.LoadError, match="exists"):
        await load_mod.run_load(sc, api="http://t", transport=httpx.ASGITransport(app=app2))


def test_never_sent_row_has_reason_and_no_times():
    row = not_sent_row(request_id="r", scenario="s", question_id="q", retrieval_setting="medium",
                       reason="not_sent:runner_runtime_cap", meta={"source": "stand-in"},
                       extra={"run_id": "x", "scheduled_start_time": "t", "offered_window_s": 5})
    assert row["status"] == "error" and row["outcome_reason"] == "not_sent:runner_runtime_cap"
    assert row["actual_send_time"] == "" and row["total_ms"] == ""
    assert row["first_token_ms"] == "" and row["scheduled_start_time"] == "t"
    assert set(row) == set(COLUMNS)


def test_orchestrator_refuses_existing_run_dir_and_uncalibrated(tmp_path):
    from paperrelay.experiments import run as run_mod

    sc = parse_scenario(scenario_raw())
    with pytest.raises(run_mod.RunError, match="calibration"):
        run_mod.orchestrate(sc, run_id="x", evidence_root=tmp_path, api_port=1, standin_port=2,
                            health_timeout_s=1, python="python")
    (tmp_path / "stand-in" / "x").mkdir(parents=True)
    with pytest.raises(run_mod.RunError, match="already exists"):
        run_mod.orchestrate(sc, run_id="x", evidence_root=tmp_path, api_port=1, standin_port=2,
                            health_timeout_s=1, python="python", allow_uncalibrated=True)


def test_child_env_forces_standin_and_drops_key(tmp_path, monkeypatch):
    from paperrelay.experiments import run as run_mod

    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    sc = parse_scenario(scenario_raw(api_env={"PAPERRELAY_RETRY_MAX": "0",
                                              "PAPERRELAY_DAILY_STATE": "elsewhere.json"}))
    env = run_mod.child_env(sc, run_id="r1", run_dir=tmp_path / "r1", evidence_root=tmp_path,
                            standin_port=8100)
    assert "GROQ_API_KEY" not in env
    assert env["PAPERRELAY_PROVIDER"] == "standin" and env["PAPERRELAY_ADMIN"] == "1"
    assert env["PAPERRELAY_DAILY_STATE"] == str(tmp_path / "r1" / "daily_budget.json")
    assert env["PAPERRELAY_RETRY_MAX"] == "0" and env["PAPERRELAY_RUN_ID"] == "r1"
    assert "GROQ_API_KEY" not in run_mod.public_env(env)


def test_checked_in_scenarios_parse_and_retry_pair_differs_only_in_retry_max():
    from paperrelay.config import REPO_ROOT
    from paperrelay.experiments.scenario import load_scenario

    names = {"steady", "burst", "sustained_overload", "limit_halved", "daily_exhausted",
             "retry_on", "retry_off", "tool_faults"}
    scen = {n: load_scenario(REPO_ROOT / "experiments" / "scenarios" / f"{n}.json")
            for n in names}
    assert all(s.requests <= 80 and s.max_runtime_s <= 180 for s in scen.values())
    assert scen["burst"].window_s > 0
    assert scen["limit_halved"].control_actions
    on, off = dict(scen["retry_on"].raw), dict(scen["retry_off"].raw)
    for d in (on, off):
        d.pop("name"), d.pop("description")
    env_on, env_off = on.pop("api_env"), off.pop("api_env")
    assert on == off
    assert {k for k in env_on if env_on[k] != env_off.get(k)} == {"PAPERRELAY_RETRY_MAX"}


async def test_abandoned_request_joins_server_record_over_real_socket(tmp_path):
    """Real uvicorn socket + the real API app with a scripted provider: the client deadline
    abandons the request, the server cancels it, and the row still carries the tokens of
    the completed provider call from requests.jsonl (joined by request_id)."""
    import socket

    import uvicorn

    from paperrelay.api.main import Components, create_app
    from paperrelay.config import Settings
    from paperrelay.contracts import Usage
    from paperrelay.measure.evidence import EvidenceLogger
    from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
    from tests.fakes import (
        DRAFT_OK,
        INTERP,
        ScriptedProvider,
        Step,
        happy_script,
        synthetic_retriever,
    )

    provider = ScriptedProvider(happy_script(
        interpret=[Step(INTERP, usage=Usage(prompt_tokens=50, completion_tokens=5))],
        draft=[Step(DRAFT_OK, delay_s=10.0)]))
    logger = EvidenceLogger(tmp_path, provider.source, "load-run")
    deps = PipelineDeps(provider=provider, retriever=synthetic_retriever(), logger=logger,
                        run_info={"run_id": "load-run", "pipeline_commit": "abc",
                                  "config_hash": "h1"})
    app = create_app(Components(settings=Settings(evidence_dir=tmp_path, run_id="load-run"),
                                runner=PipelineRunner(deps)))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        sc = parse_scenario(scenario_raw(requests=1, source="offline-double",
                                         client_timeout_s=0.5, server_record_wait_s=3.0))
        result = await load_mod.run_load(sc, api=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        await task
    [row] = result.rows
    assert result.run_dir == logger.dir
    assert row["status"] == "timeout" and row["outcome_reason"] == "client_abandoned"
    assert row["tokens_in"] == 50 and row["tokens_usage_complete"] is False
    assert row["pipeline_commit"] == "abc"
