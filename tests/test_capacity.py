"""Admission queue, minute/day quota gate, reservations and daily exhaustion. No network."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from paperrelay.api.main import Components, create_app
from paperrelay.capacity.admission import AdmissionController, AdmissionRejected
from paperrelay.capacity.manager import Capacity
from paperrelay.capacity.quota import (
    Limits,
    QuotaGate,
    QuotaRejected,
    QuotaStateError,
    estimate_tokens,
)
from paperrelay.capacity.retry import RetrySettings
from paperrelay.config import Settings
from paperrelay.contracts import PipelineRequest, ProviderCall, Usage
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.prompts import PROMPT_VERSION, interpret_messages, verify_messages
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
from paperrelay.retrieval.retrieve import Passage
from tests.fakes import (
    DRAFT_OK,
    INTERP,
    QUESTION,
    VERIFY_OK,
    ScriptedProvider,
    Step,
    synthetic_retriever,
)

REAL_RUN = Path("evidence/real/real-smoke-20261112-01")


def big_limits(**kw: int) -> Limits:
    values = {"rpm": 1000, "tpm": 10**7, "rpd": 10**5, "tpd": 10**9}
    values.update(kw)
    return Limits(**values)


def capacity(tmp_path, *, conc=1, queue=4, window_s=60.0, utc_now=None, **limits) -> Capacity:
    kw = {"utc_now": utc_now} if utc_now else {}
    gate = QuotaGate(big_limits(**limits), tmp_path / "daily_budget.json", window_s=window_s, **kw)
    return Capacity(AdmissionController(conc, queue), gate)


def runner(provider, cap, *, deadline_s=5.0, retry_max=0, logger=None) -> PipelineRunner:
    return PipelineRunner(PipelineDeps(
        provider=provider, retriever=synthetic_retriever(), deadline_s=deadline_s,
        call_timeout_s=5.0, capacity=cap, logger=logger,
        retry=RetrySettings(max_retries=retry_max, base_s=0.01, cap_s=0.02),
    ))


def many(n: int, first_delay: float = 0.0) -> dict:
    """Script for n sequential happy requests; the first interpret call is slow."""
    return {"interpret": [Step(INTERP, delay_s=first_delay)] + [INTERP] * (n - 1),
            "draft": [DRAFT_OK] * n, "draft_expanded": [], "verify": [VERIFY_OK] * n}


async def ask(r: PipelineRunner, rid: str, delay: float = 0.0):
    await asyncio.sleep(delay)
    return (await r.run(PipelineRequest(question=QUESTION, request_id=rid)))[1]


def sent_call(tokens_in: int | None, tokens_out: int | None, sent: bool = True) -> ProviderCall:
    return ProviderCall(request_id="r", stage="draft", provider="p", source="offline-double",
                        model="m", status="ok", sent=sent, streamed=True, send_offset_ms=0,
                        total_ms=1, usage=Usage(prompt_tokens=tokens_in,
                                                completion_tokens=tokens_out))


# --------------------------------------------------------------------------- admission

async def test_queue_overflow_is_rate_limited_and_queued_request_waits(tmp_path):
    cap = capacity(tmp_path, conc=1, queue=1)
    r = runner(ScriptedProvider(many(2, first_delay=0.4)), cap)
    a, b, c = await asyncio.gather(ask(r, "a"), ask(r, "b", 0.05), ask(r, "c", 0.1))
    assert a.status == "answered" and a.queue_wait_ms < 50 and a.queue_peak_observed == 0
    assert b.status == "answered" and b.queue_wait_ms >= 250 and b.queue_peak_observed == 1
    assert c.status == "rate_limited" and c.reason == "admission_queue_full"
    assert c.provider_calls == 0
    assert (cap.admission.in_flight, cap.admission.queue_depth) == (0, 0)


async def test_admission_is_fifo_and_slots_are_handed_over():
    adm = AdmissionController(max_concurrency=1, max_queue=5)
    await adm.acquire("holder")
    order: list[str] = []

    async def waiter(name: str) -> None:
        await adm.acquire(name)
        order.append(name)

    tasks = []
    for name in ("w1", "w2", "w3"):
        tasks.append(asyncio.create_task(waiter(name)))
        await asyncio.sleep(0)  # enqueue in this order
    await asyncio.sleep(0)
    assert adm.queue_depth == 3
    # A new arrival must not overtake the queue even when a slot frees concurrently.
    for _ in range(3):
        adm.release()
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    assert order == ["w1", "w2", "w3"]
    assert adm.in_flight == 1  # w3 holds the slot passed down the line
    adm.release()
    assert adm.in_flight == 0


async def test_cancelled_waiter_is_removed_and_a_handed_slot_is_not_leaked():
    adm = AdmissionController(max_concurrency=1, max_queue=5)
    await adm.acquire("holder")
    gone = asyncio.create_task(adm.acquire("gone"))
    await asyncio.sleep(0)
    gone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gone
    assert adm.queue_depth == 0

    # Race: the slot is handed to a waiter that is cancelled before it resumes.
    racer = asyncio.create_task(adm.acquire("racer"))
    await asyncio.sleep(0)
    adm.release()  # hands the holder's slot to racer
    racer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await racer
    assert (adm.in_flight, adm.queue_depth) == (0, 0)


async def test_disconnect_while_queued_logs_cancelled_without_provider_calls(tmp_path):
    cap = capacity(tmp_path, conc=1, queue=4)
    logger = EvidenceLogger(tmp_path, "offline-double", "t")
    r = runner(ScriptedProvider(many(1, first_delay=0.4)), cap, logger=logger)
    running = asyncio.create_task(ask(r, "a"))
    await asyncio.sleep(0.05)
    stream = r.stream(PipelineRequest(question=QUESTION, request_id="b"))
    kinds = [(await stream.__anext__()).kind.value for _ in range(2)]
    assert kinds == ["received", "queued"]
    assert cap.admission.queue_depth == 1
    await stream.aclose()  # client went away while waiting
    assert cap.admission.queue_depth == 0
    assert (await running).status == "answered"
    records = {json.loads(x)["request_id"]: json.loads(x)
               for x in (logger.dir / "requests.jsonl").read_text().splitlines()}
    assert records["b"]["status"] == "cancelled" and records["b"]["phase_at_end"] == "queued"
    assert records["b"]["provider_calls"] == 0
    assert cap.admission.in_flight == 0


async def test_deadline_while_queued_is_queue_deadline_timeout(tmp_path):
    cap = capacity(tmp_path, conc=1, queue=4)
    slow = runner(ScriptedProvider(many(1, first_delay=0.8)), cap, deadline_s=5.0)
    impatient = runner(ScriptedProvider(many(1)), cap, deadline_s=0.3)
    a, b = await asyncio.gather(ask(slow, "a"), ask(impatient, "b", 0.05))
    assert a.status == "answered"
    assert b.status == "timeout" and b.reason == "queue_deadline"
    assert 200 <= b.queue_wait_ms <= 700 and b.provider_calls == 0
    assert cap.admission.queue_depth == 0


# --------------------------------------------------------------------------- minute gate

async def test_minute_gate_waits_when_the_wait_fits_the_deadline(tmp_path):
    gate = QuotaGate(big_limits(rpm=1), window_s=0.4)
    first = time.perf_counter()
    await gate.reserve("r1", 10, first + 5)
    waits: list[tuple[float, str]] = []
    await gate.reserve("r2", 10, time.perf_counter() + 5, lambda w, s: waits.append((w, s)))
    elapsed = time.perf_counter() - first
    assert waits and waits[0][1] == "minute_requests"
    assert elapsed >= 0.35  # waited for the first reservation to leave the window


async def test_minute_gate_rejects_instead_of_waiting_past_the_deadline(tmp_path):
    gate = QuotaGate(big_limits(rpm=1), window_s=10.0)
    await gate.reserve("r1", 10, time.perf_counter() + 5)
    t = time.perf_counter()
    with pytest.raises(QuotaRejected) as info:
        await gate.reserve("r2", 10, t + 0.2)
    assert info.value.reason == "minute_budget_wait_exceeds_deadline"
    assert time.perf_counter() - t < 0.15  # decided up front, not after sleeping


async def test_token_reservation_is_reconciled_to_reported_usage(tmp_path):
    gate = QuotaGate(big_limits(tpm=1000), tmp_path / "d.json", window_s=30.0)
    res = await gate.reserve("r1", 800, time.perf_counter() + 5)
    # A 300-token call does not fit next to an 800-token reservation...
    with pytest.raises(QuotaRejected):
        await gate.reserve("r2", 300, time.perf_counter() + 0.2)
    # ...but once the provider reports the real use (200), it does, without waiting.
    gate.reconcile(res, sent_call(150, 50))
    assert gate.minute_usage() == (1, 200) and gate.day_tokens == 200
    t = time.perf_counter()
    await gate.reserve("r2", 300, t + 0.2)
    assert time.perf_counter() - t < 0.1
    assert json.loads((tmp_path / "d.json").read_text())["tokens"] == 500


async def test_unknown_usage_keeps_reservation_and_unsent_attempt_is_refunded(tmp_path):
    gate = QuotaGate(big_limits(), tmp_path / "d.json")
    unknown = await gate.reserve("r", 800, time.perf_counter() + 5)
    gate.reconcile(unknown, sent_call(None, 12))  # partial usage counts as unknown
    gate.reconcile(unknown, sent_call(1, 1))  # settling twice changes nothing
    assert (gate.day_requests, gate.day_tokens) == (1, 800)
    assert gate.minute_usage() == (1, 800) and gate.unknown_usage_kept == 1
    cancelled = await gate.reserve("r", 500, time.perf_counter() + 5)
    gate.reconcile(cancelled, None)  # cancelled mid-call: keep
    assert gate.day_tokens == 1300
    refused = await gate.reserve("r", 400, time.perf_counter() + 5)
    gate.reconcile(refused, sent_call(None, None, sent=False))  # never left the process
    assert (gate.day_requests, gate.day_tokens) == (2, 1300)


def test_token_estimate_is_conservative_against_real_reported_prompts():
    """The 3.0 chars/token estimate must not undershoot the prompt tokens Groq reported
    for the real smoke run (interpret and verify prompts are reconstructible)."""
    meta = json.loads((REAL_RUN / "run_meta.json").read_text(encoding="utf-8"))
    if meta["prompt_version"] != PROMPT_VERSION:
        pytest.skip("prompts changed since the real run")
    calls = [json.loads(x) for x in (REAL_RUN / "calls.jsonl").read_text().splitlines()]
    requests = [json.loads(x) for x in (REAL_RUN / "requests.jsonl").read_text().splitlines()]
    checked = 0
    for req in requests:
        by_stage = {c["stage"]: c for c in calls if c["request_id"] == req["request_id"]}
        prompt = estimate_tokens(interpret_messages(req["question"]), 0, 3.0)
        assert prompt >= by_stage["interpret"]["usage"]["prompt_tokens"]
        checked += 1
        if "verify" in by_stage:
            from paperrelay.pipeline.prompts import DraftClaim
            claims = [DraftClaim(text=c["text"], citation_ids=c["citation_ids"])
                      for c in req["claims"]]
            cited = {c["chunk_id"]: Passage(c["chunk_id"], c["arxiv_version"], c["page"], 0, 0,
                                            c["passage"], 0.0) for c in req["citations"]}
            prompt = estimate_tokens(verify_messages(claims, cited), 0, 3.0)
            assert prompt >= by_stage["verify"]["usage"]["prompt_tokens"]
            checked += 1
    assert checked == 3


async def test_lowered_minute_limit_applies_mid_run_and_raised_limit_wakes_waiters(tmp_path):
    gate = QuotaGate(big_limits(rpm=4), window_s=2.0)
    for i in range(2):
        await gate.reserve(f"r{i}", 1, time.perf_counter() + 5)
    gate.set_limits(rpm=2)  # halved: the window is now full
    assert gate.limits.rpm == 2 and gate.limits.tpm == 10**7 and gate.limits_applied_utc
    with pytest.raises(QuotaRejected):
        await gate.reserve("r2", 1, time.perf_counter() + 0.3)
    t = time.perf_counter()
    waiting = asyncio.create_task(gate.reserve("r3", 1, t + 5))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    gate.set_limits(rpm=10)
    await waiting
    assert time.perf_counter() - t < 1.0  # woke on the change, not after the 2 s window


# --------------------------------------------------------------------------- day window

async def test_daily_state_survives_restart_and_resets_on_new_utc_day(tmp_path):
    path = tmp_path / "daily_budget.json"
    day1 = datetime(2026, 11, 12, 23, 59, tzinfo=UTC)
    gate = QuotaGate(big_limits(rpd=5), path, utc_now=lambda: day1)
    for _ in range(3):
        res = await gate.reserve("r", 100, time.perf_counter() + 5)
        gate.reconcile(res, sent_call(40, 10))
    restarted = QuotaGate(big_limits(rpd=5), path, utc_now=lambda: day1)
    assert (restarted.day_requests, restarted.day_tokens) == (3, 150)
    assert restarted.admit("x", 2) and not restarted.admit("y", 1)
    day2 = day1 + timedelta(minutes=2)
    next_day = QuotaGate(big_limits(rpd=5), path, utc_now=lambda: day2)
    assert (next_day.day, next_day.day_requests, next_day.day_tokens) == ("2026-11-13", 0, 0)
    assert json.loads(path.read_text())["date_utc"] == "2026-11-13"


def test_corrupt_daily_state_is_an_error_not_a_silent_reset(tmp_path):
    path = tmp_path / "daily_budget.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(QuotaStateError):
        QuotaGate(big_limits(), path)


async def test_day_holds_prevent_joint_overrun(tmp_path):
    gate = QuotaGate(big_limits(rpd=10))
    assert gate.admit("a", 6)
    assert not gate.admit("b", 6)  # 6 held + 6 > 10
    await gate.reserve("a", 1, time.perf_counter() + 5)  # consumes a's own hold
    assert gate.snapshot()["day"]["held_requests"] == 5
    with pytest.raises(QuotaRejected) as info:
        for _ in range(5):  # an unadmitted caller cannot use a's held capacity
            await gate.reserve("other", 1, time.perf_counter() + 5)
    assert info.value.reason == "daily_budget_exhausted"
    assert gate.day_requests == 5  # a's 1 + the 4 'other' calls that fitted unheld capacity
    gate.release("a")
    assert not gate.admit("b", 6) and gate.admit("b", 5)


# --------------------------------------------------------------------------- exhaustion

async def test_upstream_daily_429_ends_inflight_and_queued_requests(tmp_path):
    cap = capacity(tmp_path, conc=1, queue=4)
    script = {"interpret": [Step("429_day", delay_s=0.3, retry_after="30")] + [INTERP] * 3,
              "draft": [DRAFT_OK] * 3, "draft_expanded": [], "verify": [VERIFY_OK] * 3}
    r = runner(ScriptedProvider(script), cap, retry_max=2)
    a, b, c = await asyncio.gather(ask(r, "a"), ask(r, "b", 0.05), ask(r, "c", 0.1))
    assert a.status == "rate_limited" and a.reason == "upstream_429_daily:interpret"
    assert a.retries == 0 and a.upstream_429_count == 1  # a day limit is never retried
    for queued in (b, c):
        assert queued.status == "rate_limited" and queued.reason == "daily_budget_exhausted"
        assert queued.provider_calls == 0 and "budget for today" in queued.message
    late = await ask(r, "d")  # new arrivals are refused until the advertised reset
    assert late.status == "rate_limited" and late.reason == "daily_budget_exhausted"
    assert json.loads((tmp_path / "daily_budget.json").read_text())["blocked_until_utc"]


async def test_local_day_exhaustion_mid_request_ends_inflight_and_queued(tmp_path):
    cap = capacity(tmp_path, conc=1, queue=4, rpd=20)
    script = {"interpret": [INTERP] * 2, "draft": [Step(DRAFT_OK, delay_s=0.4), DRAFT_OK],
              "draft_expanded": [], "verify": [VERIFY_OK] * 2}
    r = runner(ScriptedProvider(script), cap)

    async def lower_mid_run():
        await asyncio.sleep(0.2)  # request a is inside its draft call
        cap.quota.set_limits(rpd=2)

    a, b, _ = await asyncio.gather(ask(r, "a"), ask(r, "b", 0.05), lower_mid_run())
    assert a.status == "rate_limited" and a.reason == "daily_budget_exhausted"
    assert a.provider_calls == 2  # interpret + draft ran; verify was refused locally
    assert b.status == "rate_limited" and b.reason == "daily_budget_exhausted"
    assert b.provider_calls == 0
    assert cap.admission.in_flight == 0 and cap.quota.snapshot()["day"]["held_requests"] == 0


async def test_admission_requires_day_budget_for_worst_case_including_retries(tmp_path):
    cap = capacity(tmp_path, conc=2, queue=2, rpd=7)
    r = runner(ScriptedProvider(many(2)), cap, retry_max=2)  # worst case 4 + 2 = 6 calls
    first = await ask(r, "a")
    assert first.status == "answered" and cap.quota.day_requests == 3
    second = await ask(r, "b")  # 3 used + 6 > 7
    assert second.status == "rate_limited" and second.reason == "daily_budget_insufficient"
    assert second.provider_calls == 0


# --------------------------------------------------------------------------- API

def api(tmp_path, admin: bool) -> tuple[httpx.AsyncClient, Capacity]:
    cap = capacity(tmp_path, conc=3, queue=7, rpm=24, tpm=6000, rpd=900, tpd=150000)
    deps = PipelineDeps(provider=ScriptedProvider(many(1)), retriever=synthetic_retriever(),
                        capacity=cap, retry=RetrySettings(max_retries=2))
    app = create_app(Components(settings=Settings(evidence_dir=tmp_path, run_id="t",
                                                  admin=admin), runner=PipelineRunner(deps)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), cap


async def test_capacity_endpoint_reports_contract_shape(tmp_path):
    client, _ = api(tmp_path, admin=False)
    async with client:
        body = (await client.get("/capacity")).json()
    assert {"queue_depth", "in_flight", "max_concurrency", "max_queue", "limits", "minute",
            "day", "retry"} <= set(body)
    assert body["limits"] == {"rpm": 24, "tpm": 6000, "rpd": 900, "tpd": 150000}
    assert (body["max_concurrency"], body["max_queue"]) == (3, 7)
    assert {"requests", "tokens"} <= set(body["minute"])
    assert {"date_utc", "requests", "tokens"} <= set(body["day"])
    assert body["retry"]["max"] == 2


async def test_admin_limits_requires_flag_and_applies_partial_update(tmp_path):
    client, cap = api(tmp_path, admin=False)
    async with client:
        assert (await client.post("/admin/limits", json={"rpm": 12})).status_code == 403
    assert cap.quota.limits.rpm == 24
    client, cap = api(tmp_path / "admin", admin=True)
    async with client:
        resp = await client.post("/admin/limits", json={"rpm": 12, "tpm": 3000})
        assert (await client.post("/admin/limits", json={"rpm": 0})).status_code == 422
        after = (await client.get("/capacity")).json()
    assert resp.status_code == 200
    body = resp.json()
    assert body["limits"] == {"rpm": 12, "tpm": 3000, "rpd": 900, "tpd": 150000}
    assert datetime.fromisoformat(body["applied_utc"]).tzinfo is not None
    assert after["limits"]["rpm"] == 12


async def test_fail_queued_reports_reason_to_waiters():
    adm = AdmissionController(max_concurrency=1, max_queue=3)
    await adm.acquire("holder")
    waiter = asyncio.create_task(adm.acquire("w"))
    await asyncio.sleep(0)
    assert adm.fail_queued("daily_budget_exhausted") == 1
    with pytest.raises(AdmissionRejected) as info:
        await waiter
    assert info.value.reason == "daily_budget_exhausted"
