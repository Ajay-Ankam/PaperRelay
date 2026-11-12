"""Groq adapter against mocked HTTP streams; no network."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from paperrelay.providers.base import (
    CallContext,
    ChatRequest,
    ProviderHTTPError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderTimeout,
)
from paperrelay.providers.groq import GroqProvider

FAKE_KEY = "test-key-not-real-0000"
REQ = ChatRequest("draft", [{"role": "user", "content": "hi"}], 300, "draft",
                  {"type": "object", "properties": {}, "required": [], "additionalProperties": False})


def sse(*chunks: dict, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def provider_with(handler, json_mode="stream_prompt") -> tuple[GroqProvider, list]:
    seen: list[httpx.Request] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return GroqProvider(client, FAKE_KEY, "openai/gpt-oss-20b", json_mode=json_mode), seen


def ctx(records: list, timeout_s: float = 5.0) -> CallContext:
    return CallContext("r1", time.perf_counter(), timeout_s, record=records.append)


def content_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


async def test_stream_parses_content_ignores_reasoning_and_reads_usage():
    async def handler(_):
        return httpx.Response(200, content=sse(
            {"choices": [{"delta": {"reasoning": "private chain of thought"}}]},
            content_chunk('{"a": '), content_chunk("1}"),
            {"choices": [], "x_groq": {"usage": {"prompt_tokens": 11, "completion_tokens": 5}}},
        ), headers={"x-ratelimit-remaining-tokens": "7000", "set-cookie": "secret"})

    provider, seen = provider_with(handler)
    records: list = []
    result = await provider.complete(REQ, ctx(records))
    assert result.content == '{"a": 1}'
    assert "private chain" not in result.content
    assert result.call.usage.prompt_tokens == 11 and result.call.usage.completion_tokens == 5
    assert result.call.streamed and result.call.provider_first_content_ms is not None
    assert result.call.rate_limit_headers == {"x-ratelimit-remaining-tokens": "7000"}
    body = json.loads(seen[0].content)
    assert body["model"] == "openai/gpt-oss-20b" and body["stream"] is True
    assert body["max_completion_tokens"] == 300 and "max_tokens" not in body
    assert body["include_reasoning"] is False and "reasoning_format" not in body
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in json.dumps([r.model_dump(mode="json") for r in records])


async def test_missing_usage_is_unknown():
    async def handler(_):
        return httpx.Response(200, content=sse(content_chunk("{}")))

    provider, _ = provider_with(handler)
    result = await provider.complete(REQ, ctx([]))
    assert result.call.usage.prompt_tokens is None and not result.call.usage.complete


@pytest.mark.parametrize("body", [
    b"data: {not json}\n\n",
    sse(content_chunk("{}"), done=False),
    sse({"choices": [{"delta": {}}]}),
    b"data: [1, 2]\n\n",
])
async def test_malformed_stream_raises_and_records(body):
    async def handler(_):
        return httpx.Response(200, content=body)

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderMalformed):
        await provider.complete(REQ, ctx(records))
    assert records[-1].status == "malformed"


async def test_error_event_inside_stream_is_upstream_error():
    async def handler(_):
        return httpx.Response(200, content=sse({"error": {"message": "overloaded"}}))

    provider, _ = provider_with(handler)
    with pytest.raises(ProviderHTTPError):
        await provider.complete(REQ, ctx([]))


async def test_429_records_redacted_headers_and_retry_after():
    async def handler(_):
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit reached for requests", "type": "requests"}},
            headers={"retry-after": "7", "x-ratelimit-remaining-requests": "0",
                     "x-ratelimit-reset-requests": "2m59s", "set-cookie": "s=1"},
        )

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderRateLimited) as info:
        await provider.complete(REQ, ctx(records))
    call = info.value.call
    assert call.status == "rate_limited" and call.http_status == 429
    assert call.retry_after == "7"
    assert "set-cookie" not in call.rate_limit_headers
    assert call.rate_limit_headers["x-ratelimit-reset-requests"] == "2m59s"
    assert "Rate limit reached" in call.error
    assert records == [call]


async def test_http_500_is_upstream_error():
    async def handler(_):
        return httpx.Response(500, text="oops")

    provider, _ = provider_with(handler)
    with pytest.raises(ProviderHTTPError) as info:
        await provider.complete(REQ, ctx([]))
    assert info.value.call.http_status == 500


async def test_slow_response_times_out():
    async def handler(_):
        await asyncio.sleep(1.0)
        return httpx.Response(200, content=sse(content_chunk("{}")))

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderTimeout):
        await provider.complete(REQ, ctx(records, timeout_s=0.1))
    assert records[-1].status == "timeout"


@pytest.mark.parametrize("exc, sent", [
    (httpx.ConnectTimeout("connect"), False),
    (httpx.PoolTimeout("pool"), False),
    (httpx.ReadTimeout("read"), True),
])
async def test_httpx_timeouts_are_timeouts_and_connect_failures_were_not_sent(exc, sent):
    async def handler(request):
        raise type(exc)(str(exc), request=request)

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderTimeout):
        await provider.complete(REQ, ctx(records))
    assert records[-1].status == "timeout" and records[-1].sent is sent


async def test_connect_error_is_upstream_error_not_sent():
    async def handler(request):
        raise httpx.ConnectError("refused", request=request)

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderHTTPError):
        await provider.complete(REQ, ctx(records))
    assert records[-1].sent is False


async def test_finish_reason_is_recorded_from_stream():
    async def handler(_):
        return httpx.Response(200, content=sse(
            content_chunk('{"a": 1}'), {"choices": [{"delta": {}, "finish_reason": "stop"}]}))

    provider, _ = provider_with(handler)
    result = await provider.complete(REQ, ctx([]))
    assert result.call.finish_reason == "stop"


async def test_reasoning_exhausting_budget_is_malformed_with_length_finish():
    async def handler(_):
        return httpx.Response(200, content=sse(
            {"choices": [{"delta": {"reasoning": "thinking"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]}))

    provider, _ = provider_with(handler)
    records: list = []
    with pytest.raises(ProviderMalformed):
        await provider.complete(REQ, ctx(records))
    assert records[-1].finish_reason == "length"


async def test_cancellation_is_recorded():
    async def handler(_):
        await asyncio.sleep(5.0)
        return httpx.Response(200, content=sse(content_chunk("{}")))

    provider, _ = provider_with(handler)
    records: list = []
    task = asyncio.create_task(provider.complete(REQ, ctx(records)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records[-1].status == "cancelled"


async def test_strict_schema_mode_is_non_streaming_structured_output():
    async def handler(_):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        })

    provider, seen = provider_with(handler, json_mode="strict_schema")
    result = await provider.complete(REQ, ctx([]))
    body = json.loads(seen[0].content)
    assert "stream" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert result.content == '{"ok": true}'
    assert not result.call.streamed and result.call.provider_first_content_ms is None
    assert result.call.usage.complete
