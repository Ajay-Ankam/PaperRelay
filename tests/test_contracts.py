"""Contract schema validation – round-trip and boundary checks."""

import pytest
from pydantic import ValidationError

from paperrelay.contracts import (
    Citation,
    EventKind,
    PipelineEvent,
    PipelineRequest,
    PipelineResult,
    ProviderResult,
)


def test_provider_result_requires_status():
    with pytest.raises(ValidationError):
        ProviderResult(request_id="x", status="unknown_status", total_ms=100)


def test_provider_result_ok():
    r = ProviderResult(request_id="abc", status="ok", total_ms=250.0)
    assert r.retries == 0
    assert r.upstream_429_count == 0
    assert r.provider_first_token_ms is None


def test_pipeline_request_defaults():
    req = PipelineRequest(question="What is attention?")
    assert req.retrieval_setting == "default"
    assert req.deadline_ms == 30_000.0
    assert len(req.request_id) > 0


def test_pipeline_result_refused():
    res = PipelineResult(request_id="r1", refused=True)
    assert res.answer is None
    assert res.citations == []


def test_citation_requires_passage():
    with pytest.raises(ValidationError):
        Citation(arxiv_version="1706.03762v7")


def test_citation_ok():
    c = Citation(arxiv_version="1706.03762v7", page=3, passage="Attention is all you need.")
    assert c.page == 3


def test_event_kind_answer_token_is_not_queued():
    assert EventKind.ANSWER_TOKEN != EventKind.QUEUED


def test_pipeline_event_round_trip():
    ev = PipelineEvent(request_id="r1", kind=EventKind.DONE, ts_ms=512.0)
    dumped = ev.model_dump()
    ev2 = PipelineEvent(**dumped)
    assert ev2.kind == EventKind.DONE
    assert ev2.ts_ms == 512.0


def test_pipeline_event_payload_defaults_empty():
    ev = PipelineEvent(request_id="r2", kind=EventKind.QUEUED, ts_ms=0.0)
    assert ev.payload == {}
