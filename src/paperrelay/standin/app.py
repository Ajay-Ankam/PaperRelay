"""Stand-in HTTP service: OpenAI/Groq-compatible chat completions plus admin endpoints.

Content is SYNTHETIC: a deterministic extractive responder (shared with the offline
double) produces schema-valid JSON for interpret/draft/verify, detected from the prompt
structure. It exercises pipeline mechanics only and is never model-quality evidence.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from paperrelay.providers.offline import OfflineExtractiveProvider, detect_stage
from paperrelay.standin.config import merge, with_calibration
from paperrelay.standin.sim import CONTENT_MODE, Plan, Rejection, Simulator

_RESPONDER = OfflineExtractiveProvider()


class DayUsage(BaseModel):
    requests: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)


def _error(status: int, message: str, type_: str, code: str,
           headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "code": code}},
                        status_code=status, headers=headers)


def _chunk(cid: str, model: str, delta: dict[str, Any], finish: str | None = None,
           usage: dict[str, int] | None = None) -> str:
    body: dict[str, Any] = {
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["x_groq"] = {"id": cid, "usage": usage}
    return f"data: {json.dumps(body)}\n\n"


class _Log:
    def __init__(self, path: Path | None):
        self.path = path

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {"utc": datetime.now(UTC).isoformat(), **record}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def create_standin_app(sim: Simulator, log_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="PaperRelay stand-in", version="1")
    app.state.sim = sim
    log = _Log(log_path)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "content_mode": CONTENT_MODE, "model": sim.config.model}

    @app.get("/admin/state")
    async def state() -> dict[str, Any]:
        return sim.state()

    @app.post("/admin/config")
    async def admin_config(update: dict[str, Any]) -> dict[str, Any]:
        try:
            new, cal = with_calibration(merge(sim.config, update))
        except (ValidationError, ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:500]) from exc
        applied = datetime.now(UTC).isoformat()
        sim.apply(new, applied)
        if cal is not None:
            sim.calibration = cal
        log.write({"kind": "config_applied", "applied_utc": applied, "update": update})
        return {"applied_utc": applied, "config": new.model_dump()}

    @app.post("/admin/day")
    async def admin_day(body: DayUsage) -> dict[str, Any]:
        """Set simulated day usage (e.g. near exhaustion) without spending real quota."""
        sim.set_day_usage(body.requests, body.tokens)
        applied = datetime.now(UTC).isoformat()
        log.write({"kind": "day_usage_set", "applied_utc": applied,
                   **body.model_dump(exclude_none=True)})
        return {"applied_utc": applied, "day": sim.state()["day"]}

    @app.post("/admin/reset")
    async def admin_reset() -> dict[str, Any]:
        sim.reset()
        return {"applied_utc": datetime.now(UTC).isoformat(), "state": sim.state()}

    @app.post("/openai/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            return _error(401, "Invalid API Key", "invalid_request_error", "invalid_api_key")
        try:
            body = await request.json()
            messages = body["messages"]
            model = body["model"]
            user = str(messages[-1]["content"])
        except (ValueError, KeyError, IndexError, TypeError):
            return _error(400, "invalid request body", "invalid_request_error",
                          "invalid_request")
        if model != sim.config.model:
            # No silent model switching: the stand-in serves exactly one configured model.
            return _error(404, f"The model `{model}` does not exist or you do not have "
                          "access to it.", "invalid_request_error", "model_not_found")
        stage = detect_stage(user)
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        max_completion = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
        outcome = sim.plan(stage, prompt_chars, max_completion)
        if isinstance(outcome, Rejection):
            headers = sim.headers()
            if outcome.retry_after_s is not None:
                headers["retry-after"] = str(outcome.retry_after_s)
            log.write({"kind": "call", "call_no": sim.calls, "stage": stage,
                       "status": outcome.status, "scope": outcome.scope,
                       "retry_after_s": outcome.retry_after_s, "inflight": sim.inflight})
            return _error(outcome.status, outcome.message, outcome.type, outcome.code, headers)
        plan = outcome
        content = json.dumps(_RESPONDER.respond(stage, user))
        usage = {"prompt_tokens": plan.prompt_tokens, "completion_tokens": plan.completion_tokens,
                 "total_tokens": plan.prompt_tokens + plan.completion_tokens}
        headers = sim.headers()
        headers["x-request-id"] = f"req_standin_{plan.call_no:08d}"
        record = {"kind": "call", "call_no": plan.call_no, "stage": stage, "status": 200,
                  "inflight_at_start": plan.inflight_at_start, "slowdown": plan.slowdown,
                  "tail": plan.tail, "first_content_ms": round(plan.first_content_s * 1000, 3),
                  "stream_ms": round(plan.stream_s * 1000, 3), "malformed": plan.malformed,
                  "usage": usage, "streamed": bool(body.get("stream"))}
        if not body.get("stream"):
            try:
                await asyncio.sleep(plan.first_content_s + plan.stream_s)
            finally:
                sim.finish()
            log.write(record)
            return JSONResponse({
                "id": f"chatcmpl-standin-{plan.call_no}", "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": usage, "x_groq": {"usage": usage},
            }, headers=headers)
        return StreamingResponse(_stream(sim, plan, content, model, usage, record, log),
                                 media_type="text/event-stream", headers=headers)

    return app


async def _stream(sim: Simulator, plan: Plan, content: str, model: str,
                  usage: dict[str, int], record: dict[str, Any], log: _Log
                  ) -> AsyncIterator[str]:
    cid = f"chatcmpl-standin-{plan.call_no}"
    n = max(1, sim.config.latency.chunks)
    size = max(1, -(-len(content) // n))
    pieces = [content[i:i + size] for i in range(0, len(content), size)]
    completed = False
    try:
        yield _chunk(cid, model, {"role": "assistant", "content": ""})
        await asyncio.sleep(plan.first_content_s)
        gap = plan.stream_s / max(1, len(pieces) - 1)
        mode = sim.config.faults.malformed_mode
        cut = max(1, len(pieces) // 2)  # malformed streams break after this many pieces
        for i, piece in enumerate(pieces):
            if i:
                await asyncio.sleep(gap)
            yield _chunk(cid, model, {"content": piece})
            if plan.malformed and i + 1 >= cut:
                if mode == "invalid_json":
                    yield 'data: {"choices": [{"delta": {"content": \n\n'
                # "truncate": the stream ends with no finish chunk and no [DONE]
                completed = True
                return
        yield _chunk(cid, model, {}, finish="stop", usage=usage)
        yield "data: [DONE]\n\n"
        completed = True
    finally:
        sim.finish()
        log.write({**record, "completed": completed})
