"""Deterministic test doubles: scripted provider and a small synthetic retriever."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from paperrelay.contracts import Source, Usage
from paperrelay.providers.base import (
    CallContext,
    ChatRequest,
    ChatResult,
    ProviderHTTPError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderTimeout,
    make_call,
)
from paperrelay.retrieval.extract import Chunk
from paperrelay.retrieval.index import BM25Index
from paperrelay.retrieval.retrieve import Retriever

C_ATTN = "1706.03762v7:p0005:c000010"
C_HEADS = "1706.03762v7:p0005:c000011"
C_MOSHI = "2410.00037v1:p0004:c000020"
C_HNET = "2507.07955v2:p0002:c000030"

CHUNKS = [
    Chunk(C_ATTN, "1706.03762v7", 5, 0, 120,
          "Scaled dot-product attention computes softmax of query key products divided by "
          "the square root of the key dimension."),
    Chunk(C_HEADS, "1706.03762v7", 5, 120, 260,
          "In this work we employ h = 8 parallel attention layers, or heads, in the base "
          "Transformer model."),
    Chunk(C_MOSHI, "2410.00037v1", 4, 0, 110,
          "Moshi models two audio streams in parallel, one for the user and one for the system."),
    Chunk(C_HNET, "2507.07955v2", 2, 0, 100,
          "H-Net learns segmentation boundaries with a dynamic chunking mechanism."),
    Chunk("2507.07955v2:p0003:c000031", "2507.07955v2", 3, 0, 80,
          "The routing module predicts boundary probabilities between adjacent bytes."),
    Chunk("2410.00037v1:p0006:c000021", "2410.00037v1", 6, 0, 90,
          "Mimi is a neural audio codec that produces discrete tokens at 12.5 Hz."),
]


def synthetic_retriever() -> Retriever:
    return Retriever({"medium": BM25Index(CHUNKS)})


INTERP = {"search_query": "attention heads base Transformer", "alternate_query":
          "parallel attention layers heads", "question_type": "factual"}
CLAIM_TEXT = "The base Transformer uses 8 parallel attention heads."
DRAFT_OK = {"status": "answer", "missing": "",
            "claims": [{"text": CLAIM_TEXT, "citation_ids": [C_HEADS]}]}
DRAFT_INSUFFICIENT = {"status": "insufficient_evidence", "claims": [], "missing": "head count"}
VERIFY_OK = {"verdicts": [{"claim_index": 0, "supported": True, "reason": "supported"}]}
VERIFY_FAIL = {"verdicts": [{"claim_index": 0, "supported": False, "reason": "not_supported"}]}
QUESTION = "How many attention heads does the base Transformer use?"


@dataclass
class Step:
    """dict -> JSON content; str -> raw content; failures: "429" (minute scope), "429_day",
    "http_error" (503), "http_401", "malformed" (bad stream)."""

    payload: Any
    delay_s: float = 0.0
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = "stop"
    retry_after: str | None = "7"


class ScriptedProvider:
    name = "scripted"
    source: Source = "offline-double"
    model = "scripted-v1"

    def __init__(self, script: dict[str, list[Any]]):
        self.script = {stage: list(items) for stage, items in script.items()}
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest, ctx: CallContext) -> ChatResult:
        self.requests.append(request)
        item = self.script[request.stage].pop(0)
        step = item if isinstance(item, Step) else Step(item)
        t_send = time.perf_counter()
        try:
            async with asyncio.timeout(ctx.timeout_s):
                await asyncio.sleep(step.delay_s)
        except TimeoutError:
            call = make_call(self, request, ctx, status="timeout", t_send=t_send, streamed=True)
            ctx.record(call)
            raise ProviderTimeout(call, "timeout") from None
        except asyncio.CancelledError:
            ctx.record(make_call(self, request, ctx, status="cancelled", t_send=t_send,
                                 streamed=True))
            raise
        if step.payload in ("429", "429_day"):
            day = step.payload == "429_day"
            headers = {"x-ratelimit-remaining-requests": "0" if day else "900"}
            if step.retry_after is not None:
                headers["retry-after"] = step.retry_after
            call = make_call(self, request, ctx, status="rate_limited", t_send=t_send,
                             streamed=True, http_status=429, headers=headers,
                             usage=step.usage, retryable=not day,
                             limit_scope="day" if day else "minute")
            ctx.record(call)
            raise ProviderRateLimited(call, "http_429")
        if step.payload in ("http_error", "http_401"):
            status = 401 if step.payload == "http_401" else 503
            call = make_call(self, request, ctx, status="error", t_send=t_send, streamed=True,
                             http_status=status, usage=step.usage, retryable=status >= 500)
            ctx.record(call)
            raise ProviderHTTPError(call, f"http_{status}")
        if step.payload == "malformed":
            call = make_call(self, request, ctx, status="malformed", t_send=t_send,
                             streamed=True, http_status=200, retryable=False)
            ctx.record(call)
            raise ProviderMalformed(call, "stream ended without [DONE]")
        content = step.payload if isinstance(step.payload, str) else json.dumps(step.payload)
        call = make_call(self, request, ctx, status="ok", t_send=t_send, streamed=True,
                         first_content=time.perf_counter(), usage=step.usage,
                         finish_reason=step.finish_reason)
        ctx.record(call)
        return ChatResult(content, call)

    def stages(self) -> list[str]:
        return [r.stage for r in self.requests]


def happy_script(**overrides: list[Any]) -> dict[str, list[Any]]:
    script: dict[str, list[Any]] = {"interpret": [INTERP], "draft": [DRAFT_OK],
                                    "draft_expanded": [], "verify": [VERIFY_OK]}
    script.update(overrides)
    return script


def kinds(events: list[Any]) -> list[str]:
    return [e.kind.value for e in events]
