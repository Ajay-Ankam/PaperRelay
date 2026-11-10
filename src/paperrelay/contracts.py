"""Shared request/result/provider/event contracts used across pipeline stages."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field  # noqa: F401 – re-exported via contracts

# ---------------------------------------------------------------------------
# Provider contracts (Groq / stand-in)
# ---------------------------------------------------------------------------

class ProviderRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    stream: bool = True
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class ProviderResult(BaseModel):
    request_id: str
    status: Literal["ok", "rate_limited", "timeout", "error"]
    provider_first_token_ms: float | None = None
    total_ms: float
    tokens_in: int | None = None
    tokens_out: int | None = None
    retries: int = 0
    # upstream 429 count even if the final status is ok
    upstream_429_count: int = 0
    outcome_reason: str | None = None


# ---------------------------------------------------------------------------
# Pipeline contracts
# ---------------------------------------------------------------------------

class PipelineRequest(BaseModel):
    question: str
    retrieval_setting: str = "default"
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    deadline_ms: float = 30_000.0


class Citation(BaseModel):
    arxiv_version: str
    page: int | None = None
    passage: str


class PipelineResult(BaseModel):
    request_id: str
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    refused: bool = False
    # Timing fields (milliseconds from request start)
    user_first_token_ms: float | None = None  # first checked answer content, never heartbeat
    total_ms: float | None = None
    provider_result: ProviderResult | None = None


# ---------------------------------------------------------------------------
# Streaming event contracts
# ---------------------------------------------------------------------------

class EventKind(str, Enum):
    QUEUED = "queued"
    RETRIEVAL_START = "retrieval_start"
    RETRIEVAL_DONE = "retrieval_done"
    DRAFT_START = "draft_start"
    VERIFICATION_PASS = "verification_pass"
    # answer_token only appears after verification_pass; never a heartbeat
    ANSWER_TOKEN = "answer_token"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class PipelineEvent(BaseModel):
    request_id: str
    kind: EventKind
    ts_ms: float  # monotonic elapsed ms since request start
    payload: dict[str, Any] = Field(default_factory=dict)
