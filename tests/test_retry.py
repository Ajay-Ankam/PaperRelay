"""Shared retry budget, backoff, non-retryable classes, tool timeouts and fault injection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from paperrelay.capacity.admission import AdmissionController
from paperrelay.capacity.faults import RetrievalFaults
from paperrelay.capacity.manager import Capacity
from paperrelay.capacity.quota import Limits, QuotaGate
from paperrelay.capacity.retry import (
    RetryBudget,
    RetrySettings,
    parse_reset_duration,
    parse_retry_after,
)
from paperrelay.contracts import PipelineRequest, Usage
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
from paperrelay.providers.ledger import CallLedger, CeilingGuardProvider
from tests.fakes import (
    DRAFT_INSUFFICIENT,
    INTERP,
    QUESTION,
    ScriptedProvider,
    Step,
    happy_script,
    kinds,
    synthetic_retriever,
)


def runner(provider, *, retry_max=2, base_s=0.01, cap_s=0.02, deadline_s=5.0,
           call_timeout_s=5.0, logger=None, ledger=None, capacity=None,
           faults=RetrievalFaults(), tool_timeout_s=5.0, min_attempt_s=0.1) -> PipelineRunner:
    return PipelineRunner(PipelineDeps(
        provider=provider, retriever=synthetic_retriever(), deadline_s=deadline_s,
        call_timeout_s=call_timeout_s, logger=logger, ledger=ledger, capacity=capacity,
        retry=RetrySettings(max_retries=retry_max, base_s=base_s, cap_s=cap_s, seed=0,
                            min_attempt_s=min_attempt_s),
        retrieval_faults=faults, tool_timeout_s=tool_timeout_s,
    ))


async def run(r: PipelineRunner, rid: str = "req-1"):
    return await r.run(PipelineRequest(question=QUESTION, request_id=rid))


def jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- backoff

async def test_retry_honours_retry_after_and_is_recorded(tmp_path):
    logger = EvidenceLogger(tmp_path, "offline-double", "t")
    provider = ScriptedProvider(happy_script(interpret=[Step("429", retry_after="0.3"), INTERP]))
    events, result = await run(runner(provider, logger=logger))
    assert result.status == "answered"
    assert result.retries == 1 and result.upstream_429_count == 1
    wait = next(e for e in events if e.kind.value == "retry_wait")
    assert wait.payload["delay_ms"] == 300.0  # retry-after dominates the 0.02 s jitter cap
    assert result.retry_wait_ms >= 280
    calls = jsonl(logger.dir / "calls.jsonl")
    assert [(c["stage"], c["attempt"], c["status"]) for c in calls[:2]] == [
        ("interpret", 1, "rate_limited"), ("interpret", 2, "ok")]
    assert calls[0]["retryable"] is True and calls[0]["limit_scope"] == "minute"
    k = kinds(events)
    assert k.index("call_end") < k.index("retry_wait") < k.index("interpret_done")


def test_jitter_is_bounded_by_cap_reproducible_and_spread():
    settings = RetrySettings(max_retries=10, base_s=0.5, cap_s=2.0, seed=3)
    for n in range(6):
        delays = []
        for i in range(200):
            budget = RetryBudget(settings, f"r{i}")
            budget.used = n
            delays.append(budget.delay_s(None))
        bound = min(2.0, 0.5 * 2 ** n)
        assert all(0 <= d <= bound for d in delays)
        assert max(delays) > 0.8 * bound and min(delays) < 0.2 * bound  # full jitter
    same = [RetryBudget(settings, "x").delay_s(None) for _ in range(2)]
    other_seed = RetryBudget(RetrySettings(max_retries=1, seed=4), "x").delay_s(None)
    assert same[0] == same[1] and other_seed != same[0]
    assert RetryBudget(settings, "x").delay_s(5.0) == 5.0  # retry-after is a floor


async def test_retry_is_skipped_when_backoff_would_pass_the_deadline(tmp_path):
    logger = EvidenceLogger(tmp_path, "offline-double", "t")
    provider = ScriptedProvider(happy_script(interpret=[Step("429", retry_after="5"), INTERP]))
    _, result = await run(runner(provider, deadline_s=1.0, logger=logger))
    assert result.status == "rate_limited" and result.reason == "upstream_429:interpret"
    assert result.retries == 0 and result.retry_wait_ms == 0
    assert result.server_total_ms < 500  # gave up at once instead of sleeping into the deadline
    record = jsonl(logger.dir / "requests.jsonl")[0]
    assert record["internal"]["retry_skipped"][0]["why"] == "deadline"


@pytest.mark.parametrize("failure, status, reason", [
    ("429_day", "rate_limited", "upstream_429_daily:interpret"),
    ("http_401", "error", "upstream_error:interpret"),
    ("malformed", "error", "malformed_upstream_stream:interpret"),
    ("not json at all", "error", "malformed_model_output:interpret"),
])
async def test_non_retryable_failures_use_no_retry(failure, status, reason):
    provider = ScriptedProvider(happy_script(interpret=[failure, INTERP]))
    _, result = await run(runner(provider, retry_max=2))
    assert (result.status, result.reason) == (status, reason)
    assert result.retries == 0 and provider.stages() == ["interpret"]


async def test_insufficient_evidence_is_a_refusal_not_a_retry():
    provider = ScriptedProvider(happy_script(
        draft=[DRAFT_INSUFFICIENT], draft_expanded=[DRAFT_INSUFFICIENT]))
    _, result = await run(runner(provider, retry_max=2))
    assert result.status == "refused" and result.retries == 0
    assert provider.stages() == ["interpret", "draft", "draft_expanded"]


async def test_retried_attempts_count_their_usage():
    provider = ScriptedProvider(happy_script(interpret=[
        Step("http_error", usage=Usage(prompt_tokens=10, completion_tokens=0)),
        Step(INTERP, usage=Usage(prompt_tokens=100, completion_tokens=20)),
    ]))
    _, result = await run(runner(provider))
    assert result.status == "answered" and result.retries == 1
    assert result.provider_calls == 4  # failed attempt is a sent call too
    assert (result.tokens_in, result.tokens_out) == (110, 20)
    assert result.unknown_usage_calls == 2  # draft and verify reported nothing


async def test_one_budget_is_shared_across_stages():
    provider = ScriptedProvider(happy_script(interpret=["http_error", INTERP],
                                             verify=["http_error", INTERP]))
    _, result = await run(runner(provider, retry_max=1))
    assert result.status == "error" and result.reason == "upstream_error:verify"
    assert result.retries == 1


async def test_call_timeout_is_retried_but_request_deadline_is_not():
    provider = ScriptedProvider(happy_script(interpret=[Step(INTERP, delay_s=1.0), INTERP]))
    events, result = await run(runner(provider, call_timeout_s=0.2))
    assert result.status == "answered" and result.retries == 1
    assert [e.payload["status"] for e in events if e.kind.value == "call_end"][0] == "timeout"
    slow = ScriptedProvider(happy_script(interpret=[Step(INTERP, delay_s=2.0), INTERP]))
    _, result = await run(runner(slow, deadline_s=0.3, call_timeout_s=5.0))
    assert result.status == "timeout" and result.reason == "request_deadline"
    assert result.retries == 0


async def test_ledger_admission_reserves_worst_case_including_retries(tmp_path):
    ledger = CallLedger(tmp_path / "call_ledger.json", "r1", 5, hold_lock=False)
    guarded = CeilingGuardProvider(ScriptedProvider(happy_script()), ledger)
    _, result = await run(runner(guarded, retry_max=2, ledger=ledger))
    assert result.reason == "local_call_ceiling_insufficient_for_request"  # needs 6
    _, result = await run(runner(guarded, retry_max=1, ledger=ledger))
    assert result.status == "answered" and ledger.used() == 3


async def test_waits_partition_server_total(tmp_path):
    gate = QuotaGate(Limits(rpm=1, tpm=10**7, rpd=10**5, tpd=10**9), window_s=0.3)
    cap = Capacity(AdmissionController(1, 4), gate)
    provider = ScriptedProvider(happy_script(interpret=[Step("429", retry_after="0.2"),
                                                        INTERP]))
    events, result = await run(runner(provider, capacity=cap))
    assert result.status == "answered" and result.retries == 1
    # rpm=1 in a 0.3 s window forces rate waits between consecutive calls.
    assert result.rate_wait_ms > 200 and result.retry_wait_ms >= 190
    waiting = result.queue_wait_ms + result.rate_wait_ms + result.retry_wait_ms
    assert waiting <= result.server_total_ms
    assert result.server_total_ms - waiting >= 0
    assert any(e.kind.value == "rate_wait" for e in events)


# --------------------------------------------------------------------------- retrieval tool

def _rid_with(faults: RetrievalFaults, first: str | None, second: str | None) -> str:
    for i in range(1000):
        rid = f"r{i}"
        if faults.decide(rid, 1) == first and faults.decide(rid, 2) == second:
            return rid
    raise AssertionError("no matching request id")


async def test_tool_timeout_is_retried_within_the_shared_budget():
    faults = RetrievalFaults.parse("timeout=0.5,seed=7")
    rid = _rid_with(faults, "timeout", None)
    events, result = await run(runner(ScriptedProvider(happy_script()), faults=faults,
                                      tool_timeout_s=0.1), rid)
    assert result.status == "answered" and result.retries == 1
    ends = [e.payload["status"] for e in events if e.kind.value == "tool_end"]
    assert ends == ["timeout", "ok"]
    _, result = await run(runner(ScriptedProvider(happy_script()), faults=faults,
                                 tool_timeout_s=0.1, retry_max=0), rid)
    assert result.status == "timeout" and result.reason == "tool_timeout:retrieval"


async def test_tool_error_ends_request_without_drafting():
    faults = RetrievalFaults.parse("error=0.5,seed=7")
    rid = _rid_with(faults, "error", None)
    provider = ScriptedProvider(happy_script())
    events, result = await run(runner(provider, faults=faults), rid)
    assert result.status == "error" and result.reason == "tool_error:retrieval"
    assert provider.stages() == ["interpret"] and result.retries == 0
    assert "answer_token" not in kinds(events)


def test_fault_injection_is_deterministic_and_close_to_configured_rates():
    faults = RetrievalFaults.parse("timeout=0.1,error=0.05,seed=7")
    decisions = [faults.decide(f"q{i}", 1) for i in range(4000)]
    assert decisions == [faults.decide(f"q{i}", 1) for i in range(4000)]
    assert 0.08 < decisions.count("timeout") / 4000 < 0.12
    assert 0.035 < decisions.count("error") / 4000 < 0.065
    other = RetrievalFaults.parse("timeout=0.1,error=0.05,seed=8")
    assert decisions != [other.decide(f"q{i}", 1) for i in range(4000)]
    with pytest.raises(ValueError):
        RetrievalFaults.parse("timeout=0.7,error=0.5")
    with pytest.raises(ValueError):
        RetrievalFaults.parse("latency=1")


# --------------------------------------------------------------------------- header parsing

def test_retry_after_and_groq_reset_parsing():
    assert parse_retry_after("7") == 7.0 and parse_retry_after("0.5") == 0.5
    assert parse_retry_after(None) is None and parse_retry_after("soon") is None
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    http_date = format_datetime(now + timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(http_date, now) == 30.0
    assert parse_reset_duration("2m59.56s") == pytest.approx(179.56)
    assert parse_reset_duration("1m26.4s") == pytest.approx(86.4)
    assert parse_reset_duration("7.66s") == pytest.approx(7.66)
    assert parse_reset_duration("1h2m3s") == 3723
    assert parse_reset_duration("120ms") == pytest.approx(0.12)
    assert parse_reset_duration("") is None and parse_reset_duration("abc") is None
