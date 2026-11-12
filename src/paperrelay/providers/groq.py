"""Groq OpenAI-compatible chat completions over raw HTTP (no SDK, no hidden retries).

Checked against console.groq.com/docs on 2026-11-10 (pages carried no date):
- POST {base}/chat/completions; streaming is SSE `data:` lines ending with `data: [DONE]`.
- `max_completion_tokens` replaces deprecated `max_tokens`.
- gpt-oss: `include_reasoning=false` omits reasoning; `reasoning_format` is unsupported;
  `reasoning_effort` accepts low/medium/high.
- json_schema structured outputs (strict supported for gpt-oss-20b) do not support streaming.
- Streamed usage location is undocumented; usage is read from `usage` or `x_groq.usage`
  when present and recorded as unknown otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

import httpx

from paperrelay.contracts import CallStatus, Source, Usage
from paperrelay.providers.base import (
    CallContext,
    ChatRequest,
    ChatResult,
    ProviderError,
    ProviderHTTPError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderTimeout,
    classify_429,
    http_status_retryable,
    make_call,
    redact_headers,
)


class _StreamError(Exception):
    def __init__(self, message: str, upstream: bool = False, finish: str | None = None):
        super().__init__(message)
        self.upstream = upstream
        self.finish = finish  # "length" here means reasoning used the whole output budget


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    pt, ct = raw.get("prompt_tokens"), raw.get("completion_tokens")
    return Usage(
        prompt_tokens=pt if isinstance(pt, int) else None,
        completion_tokens=ct if isinstance(ct, int) else None,
    )


def _error_detail(raw: bytes) -> tuple[str, str]:
    """(short detail for the call record, full error message for classification)."""
    try:
        err = json.loads(raw).get("error", {})
        message = str(err.get("message", ""))
        detail = f"{err.get('type', '')}: {message}"
    except (ValueError, AttributeError):
        message, detail = "", "non-JSON error body"
    return detail[:200], message


async def _read_sse(resp: httpx.Response) -> tuple[str, float | None, Usage, str | None]:
    parts: list[str] = []
    first: float | None = None
    usage = Usage()
    finish: str | None = None
    done = False
    async for raw_line in resp.aiter_lines():
        line = raw_line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise _StreamError("invalid JSON in SSE data line") from exc
        if not isinstance(chunk, dict):
            raise _StreamError("SSE chunk is not an object")
        if "error" in chunk:
            raise _StreamError(f"error event in stream: {str(chunk['error'])[:200]}", upstream=True)
        reported = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
        if reported:
            usage = _usage(reported)
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise _StreamError("choices is not a list")
        for choice in choices:
            if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
            delta = choice.get("delta") if isinstance(choice, dict) else None
            piece = delta.get("content") if isinstance(delta, dict) else None
            # delta.reasoning (if any) is deliberately ignored: never surfaced.
            if isinstance(piece, str) and piece:
                if first is None:
                    first = time.perf_counter()
                parts.append(piece)
    if not done:
        raise _StreamError("stream ended without [DONE]")
    content = "".join(parts)
    if not content.strip():
        raise _StreamError(f"stream contained no content (finish_reason={finish})", finish=finish)
    return content, first, usage, finish


def _read_completion(raw: bytes) -> tuple[str, Usage, str | None]:
    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise _StreamError("unexpected completion body") from exc
    finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
    if not isinstance(content, str) or not content.strip():
        raise _StreamError(f"completion contained no content (finish_reason={finish})",
                           finish=finish)
    return content, _usage(data.get("usage")), finish


class GroqProvider:
    """Also used, unchanged, against the local stand-in (source="stand-in")."""

    name = "groq"
    source: Source = "real"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        json_mode: str = "stream_prompt",
        reasoning_effort: str = "low",
        source: Source = "real",
        name: str = "groq",
    ):
        self.source = source
        self.name = name
        self._client = client
        self._api_key = api_key
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._json_mode = json_mode
        self._reasoning_effort = reasoning_effort

    def build_body(self, request: ChatRequest) -> tuple[dict[str, Any], bool]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "max_completion_tokens": request.max_completion_tokens,
            "include_reasoning": False,
            "reasoning_effort": self._reasoning_effort,
        }
        if self._json_mode == "strict_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name, "strict": True, "schema": request.json_schema,
                },
            }
            return body, False
        body["stream"] = True
        return body, True

    def _fail(
        self, cls: type[ProviderError], request: ChatRequest, ctx: CallContext, status: CallStatus,
        t_send: float, streamed: bool, http_status: int | None, headers: dict[str, str],
        error: str, first: float | None = None, sent: bool = True, finish: str | None = None,
        limit_scope: Literal["minute", "day"] | None = None,
    ) -> ProviderError:
        if cls is ProviderRateLimited:
            retryable = limit_scope != "day"
        elif cls is ProviderMalformed:
            retryable = False  # a resend may cost tokens and would not fix a bad answer
        elif cls is ProviderHTTPError and http_status is not None and http_status != 200:
            retryable = http_status_retryable(http_status)
        else:
            retryable = True  # transport failure, in-stream upstream error, call timeout
        call = make_call(
            self, request, ctx, status=status, t_send=t_send, streamed=streamed, sent=sent,
            first_content=first, http_status=http_status, error=error, headers=headers,
            finish_reason=finish, retryable=retryable, limit_scope=limit_scope,
        )
        ctx.record(call)
        return cls(call, error)

    async def complete(self, request: ChatRequest, ctx: CallContext) -> ChatResult:
        body, streamed = self.build_body(request)
        auth = {"Authorization": f"Bearer {self._api_key}"}
        t_send = time.perf_counter()
        http_status: int | None = None
        headers: dict[str, str] = {}
        first: float | None = None
        try:
            async with asyncio.timeout(ctx.timeout_s):
                async with self._client.stream("POST", self._url, json=body, headers=auth) as resp:
                    http_status = resp.status_code
                    headers = redact_headers(resp.headers)
                    if resp.status_code != 200:
                        detail, message = _error_detail(await resp.aread())
                        limited = resp.status_code == 429
                        raise self._fail(
                            ProviderRateLimited if limited else ProviderHTTPError, request, ctx,
                            "rate_limited" if limited else "error", t_send, streamed,
                            http_status, headers, f"http_{resp.status_code}: {detail}",
                            limit_scope=classify_429(message, headers) if limited else None,
                        )
                    if streamed:
                        content, first, usage, finish = await _read_sse(resp)
                    else:
                        content, usage, finish = _read_completion(await resp.aread())
        except ProviderError:
            raise
        except _StreamError as exc:
            cls, status = (ProviderHTTPError, "error") if exc.upstream else (ProviderMalformed, "malformed")
            raise self._fail(
                cls, request, ctx, status, t_send, streamed, http_status, headers, str(exc), first,  # type: ignore[arg-type]
                finish=exc.finish,
            ) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            # Connect/pool timeouts never reached Groq; read/write timeouts may have.
            not_sent = isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout)
            raise self._fail(
                ProviderTimeout, request, ctx, "timeout", t_send, streamed, http_status, headers,
                f"{type(exc).__name__}: no complete response within {ctx.timeout_s:.1f}s",
                first, sent=not not_sent,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._fail(
                ProviderHTTPError, request, ctx, "error", t_send, streamed, http_status, headers,
                f"transport: {type(exc).__name__}", first,
                sent=not isinstance(exc, httpx.ConnectError),
            ) from exc
        except asyncio.CancelledError:
            ctx.record(make_call(
                self, request, ctx, status="cancelled", t_send=t_send, streamed=streamed,
                first_content=first, http_status=http_status, headers=headers,
                error="cancelled (deadline or client disconnect)",
            ))
            raise
        call = make_call(
            self, request, ctx, status="ok", t_send=t_send, streamed=streamed,
            first_content=first, http_status=http_status, usage=usage, headers=headers,
            finish_reason=finish,
        )
        ctx.record(call)
        return ChatResult(content, call)
