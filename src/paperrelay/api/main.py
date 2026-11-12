"""FastAPI backend: /health, /config and the streamed /ask pipeline endpoint."""

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

from paperrelay import __version__
from paperrelay.config import (
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
from paperrelay.pipeline.runner import PipelineDeps, PipelineRunner
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


def build_components(settings: Settings) -> Components:
    retriever = Retriever.from_dir(settings.index_dir, settings.manifest_path)
    logger = EvidenceLogger(settings.evidence_dir, settings.source, settings.run_id)
    client: httpx.AsyncClient | None = None
    ledger: CallLedger | None = None
    provider: ChatProvider
    if settings.provider == "groq":
        api_key = read_groq_api_key()
        if not api_key:
            raise ConfigError("GROQ_API_KEY not found in environment or .env")
        assert settings.real_call_ceiling is not None
        # One shared client per process; no transport retries (httpx default is 0).
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=settings.call_timeout_s, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
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
    else:
        provider = OfflineExtractiveProvider()

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
        "retries": "none (Milestone 2)",
        "workers": "single process; request admission holds are in-memory; the ledger "
                   "file lock makes a second real-provider process refuse to start",
    })
    deps = PipelineDeps(
        provider=provider, retriever=retriever, deadline_s=settings.deadline_s,
        call_timeout_s=settings.call_timeout_s, logger=logger, ledger=ledger, run_info=run_info,
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
            "deadline_s": s.deadline_s,
            "real_call_ceiling": s.real_call_ceiling,
            "real_calls_used": deps.ledger.used() if deps.ledger else None,
            "evidence_dir": str(deps.logger.dir) if deps.logger else None,
        }

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
            # cancels the in-flight pipeline task and logs a 'cancelled' terminal record.
            # aclosing() guarantees the runner generator's cleanup runs on cancellation.
            async with aclosing(comps.runner.stream(body)) as events:
                async for event in events:
                    yield json.dumps(event.model_dump(mode="json")) + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    return app


app = create_app()
