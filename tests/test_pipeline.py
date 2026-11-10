"""Pipeline behaviour with deterministic doubles; zero network calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperrelay.contracts import PipelineRequest, Usage
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
from paperrelay.providers.ledger import CallLedger, CeilingGuardProvider, LedgerError
from paperrelay.providers.offline import OfflineExtractiveProvider
from tests.fakes import (
    C_HEADS,
    C_HNET,
    CHUNKS,
    CLAIM_TEXT,
    DRAFT_INSUFFICIENT,
    DRAFT_OK,
    INTERP,
    QUESTION,
    VERIFY_FAIL,
    VERIFY_OK,
    ScriptedProvider,
    Step,
    happy_script,
    kinds,
    synthetic_retriever,
)


def runner_for(provider, *, deadline_s=5.0, call_timeout_s=5.0, logger=None, ledger=None):
    return PipelineRunner(PipelineDeps(
        provider=provider, retriever=synthetic_retriever(), deadline_s=deadline_s,
        call_timeout_s=call_timeout_s, logger=logger, ledger=ledger,
    ))


async def run(provider, question=QUESTION, **kw):
    return await runner_for(provider, **kw).run(PipelineRequest(question=question))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_supported_answer_is_cited_and_released_after_verification():
    provider = ScriptedProvider(happy_script())
    events, result = await run(provider)
    k = kinds(events)
    assert result.status == "answered"
    assert result.delivery == "post_verification"
    assert k[-1] == "done" and k.count("done") == 1
    assert k.index("verification_pass") < k.index("answer_token")
    assert result.citations[0].chunk_id == C_HEADS
    chunk = next(c for c in CHUNKS if c.chunk_id == C_HEADS)
    assert (result.citations[0].arxiv_version, result.citations[0].page) == ("1706.03762v7", 5)
    assert result.citations[0].passage == chunk.text
    assert "[1]" in result.answer
    assert provider.stages() == ["interpret", "draft", "verify"]
    assert not result.expansion_used


async def test_unsupported_question_refuses_after_one_bounded_expansion():
    provider = ScriptedProvider(happy_script(
        draft=[DRAFT_INSUFFICIENT], draft_expanded=[DRAFT_INSUFFICIENT]))
    events, result = await run(provider)
    k = kinds(events)
    assert result.status == "refused" and result.reason == "insufficient_evidence"
    assert "not proof" in result.message
    assert result.expansion_used
    assert provider.stages() == ["interpret", "draft", "draft_expanded"]
    starts = [e for e in events if e.kind.value == "retrieval_start"]
    assert [e.payload["top_k"] for e in starts] == [5, 10]
    assert "answer_token" not in k and "verification_start" not in k
    assert k[-1] == "refused"


async def test_expansion_can_succeed_and_still_terminates():
    provider = ScriptedProvider(happy_script(
        draft=[DRAFT_INSUFFICIENT], draft_expanded=[DRAFT_OK]))
    events, result = await run(provider)
    assert result.status == "answered" and result.expansion_used
    assert provider.stages() == ["interpret", "draft", "draft_expanded", "verify"]
    assert kinds(events).count("retrieval_start") == 2
    assert result.provider_calls == 4


async def test_no_passages_refuses_without_drafting():
    interp = {"search_query": "zzzq", "alternate_query": "qqqz", "question_type": "other"}
    provider = ScriptedProvider(happy_script(interpret=[interp]))
    _, result = await run(provider, question="xyzzy plugh frobnicate?")
    assert result.status == "refused" and result.reason == "no_passages_retrieved"
    assert provider.stages() == ["interpret"]


@pytest.mark.parametrize("bad_id", ["made-up-id", C_HNET])
async def test_invalid_or_unsupplied_citation_is_refused_before_verification(bad_id):
    draft = {"status": "answer", "missing": "",
             "claims": [{"text": CLAIM_TEXT, "citation_ids": [bad_id]}]}
    provider = ScriptedProvider(happy_script(draft=[draft]))
    events, result = await run(provider)
    assert result.status == "refused" and result.reason == "citation_validation_failed"
    assert "verify" not in provider.stages()
    assert "answer_token" not in kinds(events)


async def test_answer_without_claims_is_refused():
    provider = ScriptedProvider(happy_script(
        draft=[{"status": "answer", "claims": [], "missing": ""}]))
    _, result = await run(provider)
    assert result.reason == "citation_validation_failed"


async def test_verification_failure_leaks_no_draft_text():
    provider = ScriptedProvider(happy_script(verify=[VERIFY_FAIL]))
    events, result = await run(provider)
    assert result.status == "refused" and result.reason == "verification_failed"
    streamed = json.dumps([e.model_dump(mode="json") for e in events])
    assert CLAIM_TEXT not in streamed
    assert "answer_token" not in kinds(events)
    assert result.answer is None and result.claims == []


async def test_verifier_must_cover_every_claim():
    provider = ScriptedProvider(happy_script(verify=[{"verdicts": []}]))
    _, result = await run(provider)
    assert result.status == "error" and result.reason == "malformed_model_output:verify_coverage"


async def test_malformed_model_output_is_operational_error_not_refusal():
    provider = ScriptedProvider(happy_script(draft=["I think the answer is 8"]))
    events, result = await run(provider)
    assert result.status == "error" and result.reason == "malformed_model_output:draft"
    assert "says nothing about whether the papers" in result.message
    assert "answer_token" not in kinds(events)


async def test_verification_transport_failure_is_error_not_refusal():
    provider = ScriptedProvider(happy_script(verify=["http_error"]))
    _, result = await run(provider)
    assert result.status == "error" and result.reason == "upstream_error:verify"


async def test_request_deadline_times_out_with_terminal_event():
    # The 5 s call timeout is clamped to the 0.3 s remaining deadline, so the provider's
    # own timeout fires at the deadline; it must be reported as the request deadline.
    provider = ScriptedProvider(happy_script(interpret=[Step(INTERP, delay_s=2.0)]))
    events, result = await run(provider, deadline_s=0.3, call_timeout_s=5.0)
    assert result.status == "timeout" and result.reason == "request_deadline"
    assert kinds(events)[-1] == "timeout"
    assert result.server_total_ms < 1500


async def test_per_call_timeout_reports_stage():
    provider = ScriptedProvider(happy_script(interpret=[Step(INTERP, delay_s=2.0)]))
    _, result = await run(provider, deadline_s=5.0, call_timeout_s=0.2)
    assert result.status == "timeout" and result.reason == "provider_call_timeout:interpret"


async def test_client_disconnect_cancels_inflight_work(tmp_path):
    logger = EvidenceLogger(tmp_path, "offline-double", "t")
    provider = ScriptedProvider(happy_script(interpret=[Step(INTERP, delay_s=5.0)]))
    stream = runner_for(provider, logger=logger).stream(PipelineRequest(question=QUESTION))
    first = await stream.__anext__()
    assert first.kind.value == "received"
    await stream.aclose()  # what Starlette does when the client goes away
    requests = _jsonl(logger.dir / "requests.jsonl")
    calls = _jsonl(logger.dir / "calls.jsonl")
    assert requests[-1]["status"] == "cancelled"
    assert calls[-1]["status"] == "cancelled" and calls[-1]["stage"] == "interpret"


async def test_progress_events_precede_and_do_not_count_as_first_answer():
    provider = ScriptedProvider(happy_script(verify=[Step(VERIFY_OK, delay_s=0.3)]))
    events, result = await run(provider)
    first_progress = events[0].ts_ms
    assert first_progress < 100
    assert result.server_first_answer_emit_ms is not None
    # Ordering is the property under test; the bound only shows the answer waited for the
    # 0.3 s verifier (slack for coarse Windows timers).
    assert result.server_first_answer_emit_ms >= 250
    k = kinds(events)
    assert k.index("verification_pass") < k.index("answer_token")
    first_answer = next(e for e in events if e.kind.value == "answer_token")
    assert result.server_first_answer_emit_ms == first_answer.ts_ms


async def test_token_usage_aggregates_across_all_stages():
    provider = ScriptedProvider(happy_script(
        interpret=[Step(INTERP, usage=Usage(prompt_tokens=100, completion_tokens=20))],
        draft=[Step(DRAFT_OK, usage=Usage(prompt_tokens=300, completion_tokens=50))],
        verify=[Step(VERIFY_OK, usage=Usage(prompt_tokens=200, completion_tokens=10))],
    ))
    _, result = await run(provider)
    assert (result.tokens_in, result.tokens_out, result.unknown_usage_calls) == (600, 80, 0)


async def test_missing_usage_is_counted_as_unknown_not_zero():
    provider = ScriptedProvider(happy_script(
        interpret=[Step(INTERP, usage=Usage(prompt_tokens=100, completion_tokens=20))],
    ))
    _, result = await run(provider)
    assert result.tokens_in == 100
    assert result.unknown_usage_calls == 2


async def test_upstream_429_is_recorded_with_retry_after(tmp_path):
    logger = EvidenceLogger(tmp_path, "offline-double", "t")
    provider = ScriptedProvider(happy_script(interpret=["429"]))
    _, result = await run(provider, logger=logger)
    assert result.status == "rate_limited" and result.reason == "upstream_429:interpret"
    assert result.upstream_429_count == 1
    call = _jsonl(logger.dir / "calls.jsonl")[0]
    assert call["http_status"] == 429 and call["retry_after"] == "7"


def ledger_at(tmp_path, ceiling, run_id="r1"):
    return CallLedger(tmp_path / "call_ledger.json", run_id, ceiling, hold_lock=False)


async def test_ceiling_admission_blocks_request_that_could_exceed_budget(tmp_path):
    ledger = ledger_at(tmp_path, 5)
    inner = ScriptedProvider(happy_script(interpret=[INTERP, INTERP]))
    guarded = CeilingGuardProvider(inner, ledger)
    _, first = await run(guarded, ledger=ledger)
    assert first.status == "answered" and ledger.used() == 3
    _, second = await run(guarded, ledger=ledger)
    # 2 calls remain but a request may need 4: rejected before any call (metrics: rate_limited)
    assert second.status == "rate_limited"
    assert second.reason == "local_call_ceiling_insufficient_for_request"
    assert second.provider_calls == 0
    assert ledger_at(tmp_path, 5).used() == 3  # persisted across restarts


async def test_ceiling_guard_refuses_locally_without_sending(tmp_path):
    ledger = ledger_at(tmp_path, 1)
    inner = ScriptedProvider(happy_script())
    _, result = await run(CeilingGuardProvider(inner, ledger))
    assert result.status == "rate_limited" and result.reason == "local_call_ceiling_reached"
    assert inner.stages() == ["interpret"]
    assert result.provider_calls == 1


async def test_concurrent_admissions_cannot_oversubscribe_the_ceiling(tmp_path):
    ledger = ledger_at(tmp_path, 6)
    assert ledger.admit("a", 4)
    assert not ledger.admit("b", 4)  # only 2 unheld slots remain
    assert ledger.reserve("other") and ledger.reserve("other")  # the 2 unheld slots
    assert not ledger.reserve("other")  # cannot take request a's held slots
    for _ in range(4):
        assert ledger.reserve("a")
    ledger.release("a")
    assert ledger.used() == 6 and ledger.remaining() == 0
    assert not ledger.reserve("a")


def test_ledger_ceiling_can_be_lowered_but_not_raised_for_a_run(tmp_path):
    ledger_at(tmp_path, 8)
    assert ledger_at(tmp_path, 20).ceiling == 8
    assert ledger_at(tmp_path, 3).ceiling == 3
    assert ledger_at(tmp_path, 20).ceiling == 3
    other = ledger_at(tmp_path, 5, run_id="r2")  # a new budget needs a new run id
    assert other.ceiling == 5
    data = json.loads((tmp_path / "call_ledger.json").read_text(encoding="utf-8"))
    assert set(data["runs"]) == {"r1", "r2"}


def test_ledger_lock_refuses_a_second_process_holder(tmp_path):
    first = CallLedger(tmp_path / "call_ledger.json", "r1", 4)
    try:
        with pytest.raises(LedgerError):
            CallLedger(tmp_path / "call_ledger.json", "r1", 4)
    finally:
        first.close()
    CallLedger(tmp_path / "call_ledger.json", "r1", 4).close()  # free again after close


def test_unreadable_ledger_is_an_error_not_a_reset(tmp_path):
    (tmp_path / "call_ledger.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError):
        ledger_at(tmp_path, 4)


async def test_verifier_supported_flag_and_reason_must_agree():
    contradictory = {"verdicts": [{"claim_index": 0, "supported": True,
                                   "reason": "partially_supported"}]}
    provider = ScriptedProvider(happy_script(verify=[contradictory]))
    events, result = await run(provider)
    assert result.status == "refused" and result.reason == "verification_failed"
    assert "answer_token" not in kinds(events)


async def test_unparseable_output_cut_at_token_limit_is_reported_as_truncated():
    provider = ScriptedProvider(happy_script(
        draft=[Step('{"status": "answer", "claims": [{"text": "The base', finish_reason="length")]))
    _, result = await run(provider)
    assert result.status == "error" and result.reason == "truncated_model_output:draft"


async def test_offline_double_answers_with_verbatim_cited_sentence():
    _, result = await run(OfflineExtractiveProvider())
    assert result.status == "answered" and result.source == "offline-double"
    cited = {c.chunk_id: c.passage for c in result.citations}
    for claim in result.claims:
        assert any(claim.text in cited[cid] for cid in claim.citation_ids)


async def test_offline_double_refuses_unrelated_question():
    _, result = await run(OfflineExtractiveProvider(),
                          question="What is the boiling point of water at sea level?")
    assert result.status == "refused"
