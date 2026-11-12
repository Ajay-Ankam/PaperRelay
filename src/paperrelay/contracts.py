"""Shared request/result/provider/event contracts used across pipeline stages."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# "real" = hosted Groq; "offline-double" = deterministic local test double.
# The calibrated stand-in (Milestone 3) will use "stand-in". Never mix sources in one table.
Source = Literal["real", "offline-double", "stand-in"]


# ---------------------------------------------------------------------------
# Provider call records
# ---------------------------------------------------------------------------

class Usage(BaseModel):
    """Provider-reported usage; None means the provider did not report it (unknown, not zero)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def complete(self) -> bool:
        return self.prompt_tokens is not None and self.completion_tokens is not None


CallStatus = Literal[
    "ok", "rate_limited", "timeout", "error", "malformed", "cancelled", "refused_by_ceiling"
]


class ProviderCall(BaseModel):
    """One provider attempt. Every attempt is recorded, including failed and cancelled ones."""

    request_id: str
    stage: str
    attempt: int = 1
    provider: str
    source: Source
    model: str
    status: CallStatus
    sent: bool = True  # False when refused locally before any network I/O
    streamed: bool
    http_status: int | None = None
    send_offset_ms: float  # monotonic, from pipeline request start
    provider_first_content_ms: float | None = None  # from send; None if not streamed/no content
    total_ms: float
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None  # e.g. "stop", "length" (output cut at token limit)
    error: str | None = None
    rate_limit_headers: dict[str, str] = Field(default_factory=dict)  # allow-listed, redacted
    retry_after: str | None = None


# ---------------------------------------------------------------------------
# Pipeline request / result
# ---------------------------------------------------------------------------

class PipelineRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    retrieval_setting: str = "default"
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class Citation(BaseModel):
    chunk_id: str
    arxiv_version: str
    page: int
    passage: str


class Claim(BaseModel):
    text: str
    citation_ids: list[str]


TerminalStatus = Literal["answered", "refused", "error", "timeout", "rate_limited", "cancelled"]


class PipelineResult(BaseModel):
    """Terminal summary. answer/claims are present only when verification passed."""

    request_id: str
    status: TerminalStatus
    reason: str | None = None
    message: str | None = None
    answer: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    delivery: Literal["post_verification"] | None = None
    source: Source
    model: str
    retrieval_setting: str
    expansion_used: bool = False
    provider_calls: int = 0
    upstream_429_count: int = 0
    tokens_in: int = 0  # sum of known prompt tokens across all calls, including failed ones
    tokens_out: int = 0
    unknown_usage_calls: int = 0
    # Server-side clocks only; client-observed first_token_ms is recorded by the client.
    server_first_answer_emit_ms: float | None = None
    server_total_ms: float = 0.0


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------

class EventKind(str, Enum):
    RECEIVED = "received"
    INTERPRET_START = "interpret_start"
    INTERPRET_DONE = "interpret_done"
    RETRIEVAL_START = "retrieval_start"
    RETRIEVAL_DONE = "retrieval_done"
    EXPANSION_START = "expansion_start"
    DRAFT_START = "draft_start"
    DRAFT_DONE = "draft_done"
    VERIFICATION_START = "verification_start"
    VERIFICATION_PASS = "verification_pass"
    VERIFICATION_FAIL = "verification_fail"
    # answer_token carries checked answer text; only emitted after verification_pass
    ANSWER_TOKEN = "answer_token"
    # terminal kinds
    DONE = "done"
    REFUSED = "refused"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TERMINAL_KINDS = frozenset(
    {EventKind.DONE, EventKind.REFUSED, EventKind.ERROR, EventKind.TIMEOUT, EventKind.CANCELLED}
)


class PipelineEvent(BaseModel):
    request_id: str
    kind: EventKind
    ts_ms: float  # monotonic elapsed ms since request start (server clock)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS
