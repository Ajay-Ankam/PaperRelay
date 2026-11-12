"""Contract schema validation – boundary checks on shared types."""

import pytest
from pydantic import ValidationError

from paperrelay.contracts import (
    TERMINAL_KINDS,
    Citation,
    EventKind,
    PipelineEvent,
    PipelineRequest,
    PipelineResult,
    ProviderCall,
    Usage,
)


def test_provider_call_rejects_unknown_status():
    with pytest.raises(ValidationError):
        ProviderCall(request_id="x", stage="draft", provider="p", source="real", model="m",
                     status="weird", streamed=True, send_offset_ms=0, total_ms=1)


def test_usage_unknown_is_not_complete():
    assert not Usage().complete
    assert not Usage(prompt_tokens=3).complete
    assert Usage(prompt_tokens=3, completion_tokens=0).complete


def test_pipeline_request_bounds():
    assert PipelineRequest(question="What is attention?").retrieval_setting == "default"
    with pytest.raises(ValidationError):
        PipelineRequest(question="")
    with pytest.raises(ValidationError):
        PipelineRequest(question="x" * 2001)


def test_result_source_restricted():
    with pytest.raises(ValidationError):
        PipelineResult(request_id="r", status="refused", source="fake", model="m",
                       retrieval_setting="medium")


def test_citation_requires_provenance():
    with pytest.raises(ValidationError):
        Citation(arxiv_version="1706.03762v7", passage="x")


def test_answer_token_is_not_terminal_and_terminal_kinds_are_exhaustive():
    assert EventKind.ANSWER_TOKEN not in TERMINAL_KINDS
    assert {k.value for k in TERMINAL_KINDS} == {"done", "refused", "error", "timeout", "cancelled"}


def test_pipeline_event_round_trip():
    ev = PipelineEvent(request_id="r1", kind=EventKind.DONE, ts_ms=512.0)
    assert PipelineEvent(**ev.model_dump()).terminal
