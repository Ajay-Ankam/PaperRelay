"""Provider interface shared by Groq and deterministic test doubles."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from paperrelay.contracts import CallStatus, ProviderCall, Source, Usage

# Allow-list of response headers kept as evidence. Authorization and cookies are never kept.
_KEPT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "retry-after",
    "x-request-id",
)


def redact_headers(headers: Any) -> dict[str, str]:
    return {name: str(headers[name]) for name in _KEPT_HEADERS if name in headers}


@dataclass
class ChatRequest:
    stage: str
    messages: list[dict[str, str]]
    max_completion_tokens: int
    schema_name: str
    json_schema: dict[str, Any]


@dataclass
class CallContext:
    request_id: str
    request_t0: float  # time.monotonic() at pipeline request start
    timeout_s: float
    attempt: int = 1
    record: Callable[[ProviderCall], None] = field(default=lambda call: None)

    def offset_ms(self, t: float | None = None) -> float:
        return round(((t if t is not None else time.monotonic()) - self.request_t0) * 1000, 3)


@dataclass
class ChatResult:
    content: str
    call: ProviderCall


class ProviderError(Exception):
    """Base class; `call` is the recorded attempt that failed."""

    def __init__(self, call: ProviderCall, message: str):
        super().__init__(message)
        self.call = call


class ProviderTimeout(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


class ProviderMalformed(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    pass


class CallCeilingReached(ProviderError):
    pass


class ChatProvider(Protocol):
    name: str
    source: Source
    model: str

    async def complete(self, request: ChatRequest, ctx: CallContext) -> ChatResult: ...


def make_call(
    provider: ChatProvider,
    request: ChatRequest,
    ctx: CallContext,
    *,
    status: CallStatus,
    t_send: float,
    streamed: bool,
    sent: bool = True,
    first_content: float | None = None,
    http_status: int | None = None,
    usage: Usage | None = None,
    error: str | None = None,
    headers: dict[str, str] | None = None,
    finish_reason: str | None = None,
) -> ProviderCall:
    now = time.monotonic()
    headers = headers or {}
    return ProviderCall(
        request_id=ctx.request_id,
        stage=request.stage,
        attempt=ctx.attempt,
        provider=provider.name,
        source=provider.source,
        model=provider.model,
        status=status,
        sent=sent,
        streamed=streamed,
        http_status=http_status,
        send_offset_ms=ctx.offset_ms(t_send),
        provider_first_content_ms=(
            round((first_content - t_send) * 1000, 3) if first_content is not None else None
        ),
        total_ms=round((now - t_send) * 1000, 3),
        usage=usage or Usage(),
        finish_reason=finish_reason,
        error=error,
        rate_limit_headers=headers,
        retry_after=headers.get("retry-after"),
    )
