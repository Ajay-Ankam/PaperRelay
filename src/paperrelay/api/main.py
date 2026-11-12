"""FastAPI backend: /health, /config, /capacity, /admin/limits and the streamed /ask endpoint.

Run with ONE worker: admission, the minute gate and daily holds are in-memory and only
coordinate requests inside this process (the daily file is persisted for restarts, not
shared between concurrent workers).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from paperrelay import __version__
from paperrelay.capacity.admission import AdmissionController
from paperrelay.capacity.faults import RetrievalFaults
from paperrelay.capacity.manager import Capacity
from paperrelay.capacity.quota import Limits, QuotaGate
from paperrelay.capacity.retry import RetrySettings
from paperrelay.config import (
    STANDIN_PLACEHOLDER_KEY,
    ConfigError,
    Settings,
    config_hash,
    git_revision,
    load_settings,
    read_groq_api_key,
)
from paperrelay.contracts import PipelineRequest
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.prompts import PROMPT_VERSION
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner, max_calls_per_request
from paperrelay.providers.base import ChatProvider
from paperrelay.providers.groq import GroqProvider
from paperrelay.providers.ledger import CallLedger, CeilingGuardProvider
from paperrelay.providers.offline import OfflineExtractiveProvider
from paperrelay.retrieval.retrieve import Retriever
from paperrelay.retrieval.settings import CONFIGS


@dataclass
class Components:
    settings: Settings
    runner: PipelineRunner
    http_client: httpx.AsyncClient | None = None
    ledger: CallLedger | None = None


class LimitsUpdate(BaseModel):
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    rpd: int | None = Field(default=None, ge=1)
    tpd: int | None = Field(default=None, ge=1)


def _http_client(settings: Settings) -> httpx.AsyncClient:
    # One shared client per process; no transport retries (httpx default is 0).
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=settings.call_timeout_s, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=max(8, settings.max_concurrency * 2),
                            max_keepalive_connections=max(4, settings.max_concurrency)),
    )


def build_capacity(settings: Settings) -> Capacity:
    return Capacity(
        admission=AdmissionController(settings.max_concurrency, settings.max_queue),
        quota=QuotaGate(Limits(settings.rpm, settings.tpm, settings.rpd, settings.tpd),
                        settings.daily_state_path),
        chars_per_token=settings.chars_per_token,
    )


def retry_settings(settings: Settings) -> RetrySettings:
    return RetrySettings(settings.retry_max, settings.retry_base_s, settings.retry_cap_s,
                         settings.retry_seed, settings.retry_min_attempt_s)


def build_components(settings: Settings) -> Components:
    retriever = Retriever.from_dir(settings.index_dir, settings.manifest_path)
    logger = EvidenceLogger(settings.evidence_dir, settings.source, settings.run_id)
    client: httpx.AsyncClient | None = None
    ledger: CallLedger | None = None
    provider: ChatProvider
    retry = retry_settings(settings)
    if settings.provider == "groq":
        api_key = read_groq_api_key()
        if not api_key:
            raise ConfigError("GROQ_API_KEY not found in environment or .env")
        assert settings.real_call_ceiling is not None
        client = _http_client(settings)
        # One ledger across all real runs; raises LedgerError (server refuses to start)
        # if another process holds it or the file is unreadable.
        ledger = CallLedger(
            settings.evidence_dir / "real" / "call_ledger.json",
            settings.run_id, settings.real_call_ceiling,
        )
        provider = CeilingGuardProvider(
            GroqProvider(client, api_key, settings.groq_model, settings.groq_base_url,
                         settings.groq_json_mode, settings.reasoning_effort),
            ledger,
        )
    elif settings.provider == "standin":
        # Same adapter and HTTP/SSE parsing as real; no Groq key is read in this mode.
        client = _http_client(settings)
        provider = GroqProvider(client, STANDIN_PLACEHOLDER_KEY, settings.groq_model,
                                settings.standin_url, settings.groq_json_mode,
                                settings.reasoning_effort, source="stand-in", name="standin")
    else:
        provider = OfflineExtractiveProvider()

    capacity = build_capacity(settings)
    faults = RetrievalFaults.parse(settings.fault_retrieval)
    retrieval_cfg = {name: vars(cfg) for name, cfg in CONFIGS.items()}
    run_info = {
        "run_id": settings.run_id,
        "pipeline_commit": git_revision(),
        "config_hash": config_hash(settings, {"retrieval": retrieval_cfg, "prompt": PROMPT_VERSION}),
        "prompt_version": PROMPT_VERSION,
    }
    logger.write_meta({
        **run_info,
        "started_utc": datetime.now(UTC).isoformat(),
        "source": settings.source,
        "model": provider.model,
        "settings": settings.public(),
        "retrieval_configs": retrieval_cfg,
        "index_meta": retriever.meta,
        "capacity": {
            "max_concurrency": settings.max_concurrency, "max_queue": settings.max_queue,
            "limits": {"rpm": settings.rpm, "tpm": settings.tpm, "rpd": settings.rpd,
                       "tpd": settings.tpd},
            "limits_basis": "design-choice defaults below documented/header-advertised "
                            "values; not measured optimums",
            "daily_state": str(settings.daily_state_path),
            "daily_window": "UTC calendar day (local policy; Groq replenishes on a rolling basis)",
            "token_estimate": f"prompt chars / {settings.chars_per_token} + 4/message + "
                              "max_completion_tokens (assumption, not a tokenizer)",
            "day_state_at_start": capacity.quota.snapshot()["day"],
        },
        "retries": {**retry.public(),
                    "max_calls_per_request": max_calls_per_request(retry.max_retries),
                    "jitter": "full jitter, per-request RNG seeded from (seed, request_id)"},
        "retrieval_tool": {"timeout_s": settings.tool_timeout_s,
                           "faults": faults.spec() if faults.active else None},
        "standin_url": settings.standin_url if settings.provider == "standin" else None,
        "workers": "single process; admission, minute gate and daily holds are in-memory; "
                   "daily accounting persisted to one file; the ledger file lock makes a "
                   "second real-provider process refuse to start",
    })
    deps = PipelineDeps(
        provider=provider, retriever=retriever, deadline_s=settings.deadline_s,
        call_timeout_s=settings.call_timeout_s, logger=logger, ledger=ledger, retry=retry,
        capacity=capacity, tool_timeout_s=settings.tool_timeout_s, retrieval_faults=faults,
        run_info=run_info,
    )
    return Components(
        settings=settings, runner=PipelineRunner(deps), http_client=client, ledger=ledger,
    )


def create_app(components: Components | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = components is None
        comps = components or build_components(load_settings())
        app.state.components = comps
        try:
            yield
        finally:
            if built and comps.http_client is not None:
                await comps.http_client.aclose()
            if built and comps.ledger is not None:
                comps.ledger.close()

    app = FastAPI(title="PaperRelay", version=__version__, lifespan=lifespan)
    if components is not None:
        app.state.components = components

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/config")
    async def config(request: Request) -> dict[str, Any]:
        comps: Components = request.app.state.components
        s = comps.settings
        deps = comps.runner.deps
        return {
            "provider": s.provider,
            "source": deps.provider.source,
            "model": deps.provider.model,
            "run_id": s.run_id,
            "retrieval_setting": s.retrieval_setting,
            "retrieval_settings": deps.retriever.available(),
            "deadline_s": deps.deadline_s,
            "call_timeout_s": deps.call_timeout_s,
            "real_call_ceiling": s.real_call_ceiling,
            "real_calls_used": deps.ledger.used() if deps.ledger else None,
            "evidence_dir": str(deps.logger.dir) if deps.logger else None,
            "standin_url": s.standin_url if s.provider == "standin" else None,
            "max_concurrency": (deps.capacity.admission.max_concurrency
                                if deps.capacity else None),
            "max_queue": deps.capacity.admission.max_queue if deps.capacity else None,
            "limits": deps.capacity.quota.snapshot()["limits"] if deps.capacity else None,
            "retry": deps.retry.public(),
            "max_calls_per_request": max_calls_per_request(deps.retry.max_retries),
            "tool_timeout_s": deps.tool_timeout_s,
            "fault_retrieval": (deps.retrieval_faults.spec()
                                if deps.retrieval_faults.active else None),
            "admin_enabled": s.admin,
        }

    @app.get("/capacity")
    async def capacity(request: Request) -> dict[str, Any]:
        comps: Components = request.app.state.components
        deps = comps.runner.deps
        if deps.capacity is None:
            raise HTTPException(status_code=404, detail="capacity control not configured")
        return deps.capacity.snapshot(retry=deps.retry.public())

    @app.post("/admin/limits")
    async def admin_limits(body: LimitsUpdate, request: Request) -> dict[str, Any]:
        comps: Components = request.app.state.components
        if not comps.settings.admin:
            raise HTTPException(status_code=403, detail="admin endpoints disabled "
                                                        "(set PAPERRELAY_ADMIN=1)")
        cap = comps.runner.deps.capacity
        if cap is None:
            raise HTTPException(status_code=404, detail="capacity control not configured")
        limits = cap.quota.set_limits(body.rpm, body.tpm, body.rpd, body.tpd)
        applied = cap.quota.limits_applied_utc
        logger = comps.runner.deps.logger
        if logger is not None:  # server-side audit of the transition time
            with open(logger.dir / "limit_changes.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "kind": "limits_changed", "applied_utc": applied, "limits": vars(limits),
                    "requested": body.model_dump(exclude_none=True),
                }) + "\n")
        return {"applied_utc": applied, "limits": vars(limits)}

    @app.post("/ask")
    async def ask(body: PipelineRequest, request: Request) -> StreamingResponse:
        comps: Components = request.app.state.components
        if body.retrieval_setting == "default":
            body = body.model_copy(update={"retrieval_setting": comps.settings.retrieval_setting})
        try:
            comps.runner.validate_setting(body.retrieval_setting)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def ndjson() -> AsyncIterator[str]:
            # Starlette cancels this generator on client disconnect; the runner then
            # cancels the in-flight pipeline task (or removes it from the admission queue)
            # and logs a 'cancelled' terminal record.
            # aclosing() guarantees the runner generator's cleanup runs on cancellation.
            async with aclosing(comps.runner.stream(body)) as events:
                async for event in events:
                    yield json.dumps(event.model_dump(mode="json")) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    return app


app = create_app()
