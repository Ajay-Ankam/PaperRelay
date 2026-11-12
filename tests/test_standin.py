"""Stand-in service through a real local socket, exercised by the real GroqProvider adapter.

No Groq calls: the adapter is pointed at 127.0.0.1 with the placeholder key.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from paperrelay.api import main as api_main
from paperrelay.config import STANDIN_PLACEHOLDER_KEY, load_settings
from paperrelay.contracts import PipelineRequest
from paperrelay.pipeline.prompts import (
    INTERPRET_SCHEMA,
    InterpretOut,
    interpret_messages,
    parse_output,
)
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
from paperrelay.providers.base import (
    CallContext,
    ChatRequest,
    ProviderHTTPError,
    ProviderMalformed,
    ProviderRateLimited,
)
from paperrelay.providers.groq import GroqProvider
from paperrelay.standin.calibrate import calibrate
from paperrelay.standin.config import StandinConfig, load_config, merge
from paperrelay.standin.sim import Plan, Simulator
from tests.fakes import QUESTION, synthetic_retriever

MODEL = "openai/gpt-oss-20b"
REAL_RUN = Path("evidence/real/real-smoke-20261112-01")


def config(**overrides: Any) -> StandinConfig:
    base: dict[str, Any] = {
        "model": MODEL, "seed": 1,
        "latency": {
            "stages": {s: {"first_content_median_ms": 100, "first_content_sigma": 0,
                           "stream_median_ms": 20, "stream_sigma": 0, "completion_tokens": 50}
                       for s in ("interpret", "draft", "verify")},
            "tail_probability": 0.0, "slowdown_per_inflight": 0.0,
        },
    }
    return merge(StandinConfig.model_validate(base), overrides)


@asynccontextmanager
async def serve(sim: Simulator, log: Path | None = None):
    from paperrelay.standin.app import create_standin_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_standin_app(sim, log), log_level="warning",
                                           lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def adapter(base: str, model: str = MODEL):
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield GroqProvider(client, STANDIN_PLACEHOLDER_KEY, model, f"{base}/openai/v1",
                           source="stand-in", name="standin")


def interpret_request() -> ChatRequest:
    return ChatRequest("interpret", interpret_messages(QUESTION), 600, "interpretation",
                       INTERPRET_SCHEMA)


def ctx(records: list | None = None) -> CallContext:
    return CallContext("r1", time.perf_counter(), 10.0,
                       record=(records.append if records is not None else lambda c: None))


# --------------------------------------------------------------------------- protocol

async def test_adapter_parses_standin_stream_with_usage_and_ttft():
    async with serve(Simulator(config())) as base, adapter(base) as provider:
        result = await provider.complete(interpret_request(), ctx())
        async with httpx.AsyncClient() as raw:
            health = (await raw.get(f"{base}/health")).json()
    parsed = parse_output(result.content, InterpretOut)
    assert parsed.search_query
    call = result.call
    assert call.source == "stand-in" and call.status == "ok" and call.streamed
    assert call.usage.complete and call.usage.completion_tokens == 50
    assert 90 <= call.provider_first_content_ms < 400  # configured 100 ms, real sleep
    assert call.rate_limit_headers["x-ratelimit-limit-requests"] == "1000"
    assert health["content_mode"] == "synthetic-extractive"


async def test_full_pipeline_runs_against_standin():
    async with serve(Simulator(config())) as base, adapter(base) as provider:
        r = PipelineRunner(PipelineDeps(provider=provider, retriever=synthetic_retriever()))
        events, result = await r.run(PipelineRequest(question=QUESTION))
    assert result.status == "answered" and result.source == "stand-in"
    assert result.provider_calls == 3 and result.unknown_usage_calls == 0
    assert result.tokens_out == 150


async def test_unknown_model_is_404_not_silently_switched():
    async with serve(Simulator(config())) as base, adapter(base, "llama-3.1-8b") as provider:
        with pytest.raises(ProviderHTTPError) as info:
            await provider.complete(interpret_request(), ctx())
    assert info.value.call.http_status == 404 and info.value.call.retryable is False
    assert "does not exist" in info.value.call.error


async def test_missing_api_key_is_401():
    async with serve(Simulator(config())) as base, httpx.AsyncClient() as raw:
        resp = await raw.post(f"{base}/openai/v1/chat/completions",
                              json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "invalid_api_key"


# --------------------------------------------------------------------------- limits

async def test_minute_429_is_retryable_with_seconds_until_window_frees():
    sim = Simulator(config(limits={"rpm": 2}))
    async with serve(sim) as base, adapter(base) as provider:
        for _ in range(2):
            await provider.complete(interpret_request(), ctx())
        with pytest.raises(ProviderRateLimited) as info:
            await provider.complete(interpret_request(), ctx())
    call = info.value.call
    assert call.limit_scope == "minute" and call.retryable is True
    assert 55 <= int(call.retry_after) <= 60  # first call leaves the 60 s window in ~60 s
    assert "requests per minute (RPM)" in call.error
    assert call.rate_limit_headers["x-ratelimit-remaining-requests"] == "998"


async def test_token_minute_429_reports_tokens_header_semantics():
    sim = Simulator(config(limits={"tpm": 400}))
    async with serve(sim) as base, adapter(base) as provider:
        first = await provider.complete(interpret_request(), ctx())
        with pytest.raises(ProviderRateLimited) as info:
            await provider.complete(interpret_request(), ctx())
    assert int(first.call.rate_limit_headers["x-ratelimit-remaining-tokens"]) < 400
    assert info.value.call.limit_scope == "minute" and "(TPM)" in info.value.call.error


async def test_day_429_is_not_retryable_and_mentions_rpd():
    sim = Simulator(config(limits={"rpd": 5}))
    async with serve(sim) as base, adapter(base) as provider, httpx.AsyncClient() as raw:
        await raw.post(f"{base}/admin/day", json={"requests": 5})
        with pytest.raises(ProviderRateLimited) as info:
            await provider.complete(interpret_request(), ctx())
        state = (await raw.get(f"{base}/admin/state")).json()
    call = info.value.call
    assert call.limit_scope == "day" and call.retryable is False
    assert "requests per day (RPD)" in call.error
    assert call.rate_limit_headers["x-ratelimit-remaining-requests"] == "0"
    assert int(call.retry_after) > 0
    assert state["counters"]["429_day"] == 1 and state["content_mode"] == "synthetic-extractive"


async def test_simulated_day_rollover_resets_day_usage():
    sim = Simulator(config(limits={"rpd": 1}))
    async with serve(sim) as base, adapter(base) as provider, httpx.AsyncClient() as raw:
        await provider.complete(interpret_request(), ctx())
        with pytest.raises(ProviderRateLimited):
            await provider.complete(interpret_request(), ctx())
        resp = await raw.post(f"{base}/admin/config", json={"clock": {"day_offset_s": 86400}})
        await provider.complete(interpret_request(), ctx())
    assert resp.status_code == 200 and resp.json()["applied_utc"]
    assert sim.day_requests == 1


async def test_admin_config_partial_update_and_validation():
    sim = Simulator(config())
    async with serve(sim) as base, httpx.AsyncClient() as raw:
        ok = await raw.post(f"{base}/admin/config", json={"limits": {"rpm": 15}})
        bad = await raw.post(f"{base}/admin/config", json={"limits": {"rpm": 0}})
        unknown = await raw.post(f"{base}/admin/config", json={"limits": {"rpz": 3}})
    assert ok.status_code == 200
    assert ok.json()["config"]["limits"]["rpm"] == 15
    assert ok.json()["config"]["limits"]["tpm"] == 8000  # untouched
    assert bad.status_code == 422 and unknown.status_code == 422
    assert sim.config.limits.rpm == 15


# --------------------------------------------------------------------------- faults/timing

async def test_injected_5xx_is_retryable_and_malformed_streams_are_detected():
    async with serve(Simulator(config(faults={"error_rate": 1.0}))) as base, \
            adapter(base) as provider:
        with pytest.raises(ProviderHTTPError) as info:
            await provider.complete(interpret_request(), ctx())
    assert info.value.call.http_status == 503 and info.value.call.retryable is True
    for mode in ("truncate", "invalid_json"):
        sim = Simulator(config(faults={"malformed_rate": 1.0, "malformed_mode": mode}))
        async with serve(sim) as base, adapter(base) as provider:
            with pytest.raises(ProviderMalformed):
                await provider.complete(interpret_request(), ctx())
        assert sim.inflight == 0


async def test_concurrent_load_slows_calls_down():
    sim = Simulator(config(latency={"slowdown_per_inflight": 1.0,
                                    "slowdown_free_inflight": 1}))
    async with serve(sim) as base, adapter(base) as provider:
        solo = await provider.complete(interpret_request(), ctx())
        together = await asyncio.gather(*(provider.complete(interpret_request(), ctx())
                                          for _ in range(4)))
    slowest = max(r.call.provider_first_content_ms for r in together)
    assert solo.call.provider_first_content_ms < 250
    assert slowest >= 2.5 * solo.call.provider_first_content_ms  # 4th call: x4 multiplier
    assert sim.inflight == 0


def _plans(seed: int, n: int = 20) -> list[Plan]:
    cfg = config(seed=seed, limits={"rpm": 10**6, "tpm": 10**9, "rpd": 10**6, "tpd": 10**9},
                 latency={"tail_probability": 0.2, "tail_multiplier": 5.0,
                                     "stages": {s: {"first_content_median_ms": 100,
                                                    "first_content_sigma": 0.6,
                                                    "stream_median_ms": 20, "stream_sigma": 0.5,
                                                    "completion_tokens": 50}
                                                for s in ("interpret", "draft", "verify")}})
    sim = Simulator(cfg)
    out = []
    for _ in range(n):
        plan = sim.plan("draft", 3000, 1200)
        assert isinstance(plan, Plan)
        sim.finish()
        out.append(plan)
    return out


def test_seeded_timing_is_reproducible_and_has_a_long_tail():
    a, b, c = _plans(5), _plans(5), _plans(6)
    assert [(p.first_content_s, p.stream_s, p.tail) for p in a] == \
        [(p.first_content_s, p.stream_s, p.tail) for p in b]
    assert [p.first_content_s for p in a] != [p.first_content_s for p in c]
    tails = [p for p in _plans(5, 400) if p.tail]
    assert 40 <= len(tails) <= 120  # ~20% configured
    normal = sorted(p.first_content_s for p in _plans(5, 400) if not p.tail)
    assert max(p.first_content_s for p in tails) > 2 * normal[len(normal) // 2] * 2


# --------------------------------------------------------------------------- calibration

def test_calibration_counts_real_calls_and_lists_assumptions(tmp_path):
    cal = calibrate([REAL_RUN])
    assert cal["preliminary"] is True and cal["source_run_ids"] == ["real-smoke-20261112-01"]
    assert cal["sample_counts"] == {"interpret": 2, "draft": 2, "draft_expanded": 1,
                                    "verify": 1}
    assert cal["standin_parameters"]["draft"]["n"] == 3  # pooled with draft_expanded
    for stage, p in cal["standin_parameters"].items():
        samples = [v for m in p["pooled_from"]
                   for v in cal["measured_samples"][m]["provider_first_content_ms"]]
        assert min(samples) <= p["first_content_median_ms"] <= max(samples)
    text = " ".join(cal["assumptions"]).lower()
    for topic in ("tail", "slowdown", "error", "lognormal", "pool"):
        assert topic in text
    assert "15.6" in cal["clock_resolution_note"]
    committed = json.loads(Path("standin/calibration-v1-preliminary.json").read_text())
    regenerated = {k: v for k, v in cal.items() if k != "inputs"}
    assert {k: v for k, v in committed.items() if k != "inputs"} == regenerated


def test_calibration_refuses_non_real_evidence(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run_meta.json").write_text(json.dumps({"run_id": "x", "source": "stand-in"}))
    (run / "calls.jsonl").write_text("")
    with pytest.raises(ValueError):
        calibrate([run])


def test_default_config_loads_calibrated_stage_parameters():
    cfg, cal = load_config(Path("standin/config-default.json"))
    assert cal is not None and cal["preliminary"] is True
    assert set(cfg.latency.stages) >= {"interpret", "draft", "verify"}
    assert cfg.latency.stages["verify"].first_content_median_ms == \
        cal["standin_parameters"]["verify"]["first_content_median_ms"]


# --------------------------------------------------------------------------- API wiring

def test_standin_mode_settings_and_no_groq_key_read(monkeypatch, tmp_path):
    s = load_settings({"PAPERRELAY_PROVIDER": "standin", "PAPERRELAY_EVIDENCE_DIR": str(tmp_path),
                       "PAPERRELAY_STANDIN_URL": "http://127.0.0.1:8123/openai/v1"})
    assert s.source == "stand-in" and s.real_call_ceiling is None

    def no_key(*_a: object, **_k: object) -> str:
        raise AssertionError("stand-in mode must not read the Groq key")

    monkeypatch.setattr(api_main, "read_groq_api_key", no_key)
    monkeypatch.setattr(api_main.Retriever, "from_dir",
                        classmethod(lambda cls, *a, **k: synthetic_retriever()))
    comps = api_main.build_components(s)
    provider = comps.runner.deps.provider
    assert isinstance(provider, GroqProvider) and provider.source == "stand-in"
    assert provider._url == "http://127.0.0.1:8123/openai/v1/chat/completions"
    assert comps.ledger is None and comps.runner.deps.capacity is not None
    meta = json.loads((tmp_path / "stand-in" / s.run_id / "run_meta.json").read_text())
    assert meta["retries"]["max_calls_per_request"] == 6
    assert meta["capacity"]["limits"] == {"rpm": 24, "tpm": 6000, "rpd": 900, "tpd": 150000}
    asyncio.run(comps.http_client.aclose())  # type: ignore[union-attr]
