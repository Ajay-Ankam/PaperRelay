"""FastAPI /ask streaming and the client smoke runner, via in-process ASGI transport."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import socket
import time

import httpx
import uvicorn

from paperrelay.api.main import Components, create_app
from paperrelay.client import smoke
from paperrelay.config import Settings
from paperrelay.contracts import PipelineRequest, Usage
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
from tests.fakes import (
    DRAFT_INSUFFICIENT,
    DRAFT_OK,
    INTERP,
    QUESTION,
    ScriptedProvider,
    Step,
    happy_script,
    synthetic_retriever,
)


def app_with(provider, tmp_path):
    logger = EvidenceLogger(tmp_path, provider.source, "test-run")
    deps = PipelineDeps(provider=provider, retriever=synthetic_retriever(), logger=logger,
                        run_info={"run_id": "test-run", "pipeline_commit": "abc",
                                  "config_hash": "h1"})
    settings = Settings(evidence_dir=tmp_path, run_id="test-run")
    return create_app(Components(settings=settings, runner=PipelineRunner(deps))), logger


async def test_ask_streams_ndjson_with_single_terminal_event(tmp_path):
    app, _ = app_with(ScriptedProvider(happy_script()), tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        resp = await client.post("/ask", json={"question": "attention heads?"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in resp.text.splitlines()]
    terminal = [e for e in events if e["kind"] in ("done", "refused", "error", "timeout")]
    assert len(terminal) == 1 and events[-1] is terminal[0]
    assert events[-1]["payload"]["retrieval_setting"] == "medium"


async def test_ask_rejects_unknown_setting_and_bad_question(tmp_path):
    app, _ = app_with(ScriptedProvider(happy_script()), tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        bad_setting = await client.post("/ask", json={"question": "q", "retrieval_setting": "huge"})
        empty = await client.post("/ask", json={"question": ""})
    assert bad_setting.status_code == 422 and empty.status_code == 422


async def test_smoke_client_writes_one_results_row_per_request(tmp_path):
    provider = ScriptedProvider(happy_script(
        interpret=[INTERP, INTERP],
        draft=[happy_script()["draft"][0], DRAFT_INSUFFICIENT],
        draft_expanded=[DRAFT_INSUFFICIENT],
    ))
    app, logger = app_with(provider, tmp_path)
    args = argparse.Namespace(
        api="http://t", question=["attention heads?", "attention heads again?"],
        questions_file=None, retrieval_setting="default", scenario="smoke",
        question_id_prefix="q", max_requests=2, sleep_between_s=0.0, client_timeout_s=10.0,
        out_dir=str(tmp_path / "unused"),
    )
    rows = await smoke.run(args, transport=httpx.ASGITransport(app=app))

    with open(logger.dir / "results.csv", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames[:12] == smoke.REQUIRED_COLUMNS
        written = list(reader)
    assert len(written) == 2 == len({r["request_id"] for r in written})
    answered, refused = written
    assert answered["status"] == "ok" and answered["first_token_ms"] != ""
    assert refused["status"] == "ok" and refused["first_token_ms"] == ""
    assert refused["outcome_reason"] == "insufficient_evidence"
    assert {r["source"] for r in written} == {"offline-double"}
    assert answered["pipeline_commit"] == "abc" and answered["answer_grade"] == ""
    # scripted calls report no usage: blank, not zero
    assert answered["tokens_usage_complete"] == "False"
    assert answered["tokens_in"] == "" and answered["tokens_out"] == ""
    server_ids = {json.loads(line)["request_id"]
                  for line in (logger.dir / "requests.jsonl").read_text().splitlines()}
    assert server_ids == {r["request_id"] for r in rows}
    observations = (logger.dir / "client_observations.jsonl").read_text().splitlines()
    assert len(observations) == 2


def test_results_row_partial_usage_keeps_known_sum_and_flags_incomplete():
    obs = {"request_id": "x", "scenario": "smoke", "start_time": "t", "first_token_ms": 40.0,
           "total_ms": 90.0, "retrieval_setting": "medium", "question_id": "q1"}
    terminal = {"status": "answered", "provider_calls": 3, "unknown_usage_calls": 1,
                "tokens_in": 100, "tokens_out": 20, "source": "real"}
    row = smoke._row(obs, terminal, {}, None)
    assert (row["tokens_in"], row["tokens_out"]) == (100, 20)
    assert row["tokens_usage_complete"] is False
    assert row["answer_delivery_ms"] == 50.0


def test_results_row_for_missing_terminal_is_error():
    obs = {"request_id": "x", "scenario": "smoke", "start_time": "t", "first_token_ms": None,
           "total_ms": 12.0, "retrieval_setting": "default", "question_id": "q1",
           "client_reason": "stream_ended_without_terminal"}
    row = smoke._row(obs, None, {"source": "real"}, None)
    assert row["status"] == "error" and row["outcome_reason"] == "stream_ended_without_terminal"
    assert row["first_token_ms"] == "" and row["tokens_in"] == ""


def test_abandoned_request_is_timeout_and_keeps_server_token_counts():
    obs = {"request_id": "x", "scenario": "smoke", "start_time": "t", "first_token_ms": None,
           "total_ms": 500.0, "retrieval_setting": "medium", "question_id": "q1",
           "client_reason": "client_abandoned"}
    server = {"status": "cancelled", "provider_calls": 2, "unknown_usage_calls": 1,
              "tokens_in": 120, "tokens_out": 9, "server_total_ms": 510.0,
              "calls": [{"sent": True, "provider_first_content_ms": 0.0}]}
    row = smoke._row(obs, None, {}, server)
    assert row["status"] == "timeout" and row["outcome_reason"] == "client_abandoned"
    assert (row["tokens_in"], row["tokens_out"]) == (120, 9)  # spent tokens still count
    assert row["tokens_usage_complete"] is False
    assert row["provider_first_token_ms"] == 0.0  # a real 0.0 is kept, not blanked


async def test_client_skips_unparseable_lines_and_records_them():
    lines = [
        json.dumps({"kind": "received", "ts_ms": 1, "payload": {}}),
        "{this is not json",
        json.dumps({"kind": "done", "ts_ms": 2, "payload": {"status": "answered"}}),
    ]

    async def handler(_):
        return httpx.Response(200, content=("\n".join(lines) + "\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://t") as client:
        obs, terminal = await smoke.ask_once(client, "q", "medium", "smoke", "q1")
    assert terminal == {"status": "answered"}
    assert len(obs["parse_errors"]) == 1 and "client_reason" not in obs


async def test_real_socket_disconnect_cancels_server_pipeline(tmp_path):
    """Through a real uvicorn socket: the client times out mid-request and goes away; the
    server must cancel the in-flight provider call and record it, not keep working."""
    provider = ScriptedProvider(happy_script(
        interpret=[Step(INTERP, usage=Usage(prompt_tokens=50, completion_tokens=5))],
        draft=[Step(DRAFT_OK, delay_s=10.0)],
    ))
    app, logger = app_with(provider, tmp_path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        args = argparse.Namespace(
            api=f"http://127.0.0.1:{port}", question=["attention heads?"], questions_file=None,
            retrieval_setting="default", scenario="disconnect", question_id_prefix="d",
            max_requests=1, sleep_between_s=0.0, client_timeout_s=0.5,
            server_record_wait_s=3.0, out_dir=str(tmp_path / "unused"),
        )
        t0 = time.perf_counter()
        [row] = await smoke.run(args)
        elapsed = time.perf_counter() - t0
    finally:
        server.should_exit = True
        await task
    assert row["status"] == "timeout" and row["outcome_reason"] == "client_abandoned"
    assert elapsed < 5.0  # far below the 10 s draft delay: the server stopped waiting
    [record] = [json.loads(x) for x in (logger.dir / "requests.jsonl").read_text().splitlines()]
    assert record["status"] == "cancelled" and record["reason"] == "client_disconnected"
    calls = [json.loads(x) for x in (logger.dir / "calls.jsonl").read_text().splitlines()]
    assert [c["status"] for c in calls] == ["ok", "cancelled"]
    # tokens of the completed interpret call still count; the cancelled call's are unknown
    assert row["tokens_in"] == 50 and row["tokens_usage_complete"] is False


async def test_offline_mode_keeps_answering_back_to_back_questions(tmp_path):
    # Regression: the offline double reports no usage, so a quota gate kept every worst-case
    # reservation and refused the second question with minute_budget_wait_exceeds_deadline.
    from paperrelay.api.main import build_capacity
    from paperrelay.providers.offline import OfflineExtractiveProvider

    settings = Settings(provider="offline", evidence_dir=tmp_path, run_id="t")
    deps = PipelineDeps(provider=OfflineExtractiveProvider(), retriever=synthetic_retriever(),
                        capacity=build_capacity(settings))
    runner = PipelineRunner(deps)
    results = [(await runner.run(PipelineRequest(question=QUESTION)))[1] for _ in range(3)]
    assert [r.status for r in results] == ["answered"] * 3
    assert not list(tmp_path.rglob("daily_budget.json"))  # nothing persisted for the double


def test_quota_gate_stays_enforced_for_standin_and_real(tmp_path):
    from paperrelay.api.main import build_capacity

    for provider in ("standin", "groq"):
        settings = Settings(provider=provider, evidence_dir=tmp_path, run_id="t",
                            rpm=7, tpm=700)
        assert build_capacity(settings).quota.snapshot()["limits"]["tpm"] == 700
