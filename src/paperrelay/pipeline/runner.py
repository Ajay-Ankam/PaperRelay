"""interpret -> retrieval tool -> draft -> (at most one evidence expansion) -> verify -> delivery.

Answer text leaves the server only after verification passes. Drafts, search queries and
verifier output stay internal (server evidence logs only). Every request ends with exactly
one terminal event; a cancelled request is logged but cannot be sent to a gone client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from paperrelay.contracts import (
    Citation,
    Claim,
    EventKind,
    PipelineEvent,
    PipelineRequest,
    PipelineResult,
    ProviderCall,
    TerminalStatus,
)
from paperrelay.measure.evidence import EvidenceLogger
from paperrelay.pipeline.prompts import (
    DRAFT_SCHEMA,
    INTERPRET_SCHEMA,
    VERIFY_SCHEMA,
    DraftOut,
    InterpretOut,
    OutputFormatError,
    VerifyOut,
    draft_messages,
    interpret_messages,
    parse_output,
    verify_messages,
)
from paperrelay.providers.base import (
    CallCeilingReached,
    CallContext,
    ChatProvider,
    ChatRequest,
    ProviderError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderTimeout,
)
from paperrelay.providers.ledger import CallLedger
from paperrelay.retrieval.retrieve import Passage, Retriever

# interpret + draft + expanded draft + verify. No retries exist in this milestone.
MAX_CALLS_PER_REQUEST = 4
MAX_CLAIMS = 5
MAX_CLAIM_CHARS = 600

MESSAGES = {
    "insufficient_evidence": (
        "I could not find enough support in the retrieved passages from the three papers to "
        "answer this. That is not proof the papers never address it: the search may have "
        "missed the relevant passage."
    ),
    "no_passages_retrieved": (
        "The search found no passages from the three papers matching this question. That is "
        "not proof the papers never address it."
    ),
    "citation_validation_failed": (
        "I drafted an answer, but its citations did not check out against the retrieved "
        "passages, so I am not showing it."
    ),
    "verification_failed": (
        "I drafted an answer, but checking it against the cited passages did not support "
        "every claim, so I am not showing it."
    ),
}
OPERATIONAL = (
    "The request failed for an operational reason ({reason}). This says nothing about "
    "whether the papers answer the question."
)
_TERMINAL_KIND: dict[TerminalStatus, EventKind] = {
    "answered": EventKind.DONE,
    "refused": EventKind.REFUSED,
    "timeout": EventKind.TIMEOUT,
    "cancelled": EventKind.CANCELLED,
    "error": EventKind.ERROR,
    "rate_limited": EventKind.ERROR,
}


class RetryPolicy(Protocol):
    """Extension point for Milestone 3's bounded retry budget."""

    def retry_delay_s(self, error: ProviderError, attempt: int, remaining_s: float) -> float | None:
        ...


class NoRetry:
    def retry_delay_s(self, error: ProviderError, attempt: int, remaining_s: float) -> float | None:
        return None


@dataclass
class PipelineDeps:
    provider: ChatProvider
    retriever: Retriever
    deadline_s: float = 60.0
    call_timeout_s: float = 30.0
    logger: EvidenceLogger | None = None
    ledger: CallLedger | None = None
    retry_policy: RetryPolicy = field(default_factory=NoRetry)
    run_info: dict[str, Any] = field(default_factory=dict)


class _Terminal(Exception):
    def __init__(self, status: TerminalStatus, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _utc() -> str:
    return datetime.now(UTC).isoformat()


class _Run:
    def __init__(self, req: PipelineRequest, deps: PipelineDeps):
        self.req = req
        self.deps = deps
        self.t0 = time.perf_counter()
        self.deadline_at = self.t0 + deps.deadline_s
        self.start_utc = _utc()
        self.setting = deps.retriever.config(req.retrieval_setting).name
        self.queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()
        self.calls: list[ProviderCall] = []
        self.first_emit_ms: float | None = None
        self.expansion_used = False
        self.internal: dict[str, Any] = {}

    # ------------------------------------------------------------------ plumbing

    def _ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 3)

    def emit(self, kind: EventKind, send: bool = True, **payload: Any) -> PipelineEvent:
        event = PipelineEvent(request_id=self.req.request_id, kind=kind, ts_ms=self._ms(),
                              payload=payload)
        if send:
            self.queue.put_nowait(event)
        if self.deps.logger:
            self.deps.logger.event(event, _utc())
        return event

    def _record(self, call: ProviderCall) -> None:
        self.calls.append(call)
        if self.deps.logger:
            self.deps.logger.call(call)

    async def _model(
        self, stage: str, messages: list[dict[str, str]], schema_name: str,
        schema: dict[str, Any], out: type[BaseModel], max_tokens: int,
    ) -> Any:
        request = ChatRequest(stage, messages, max_tokens, schema_name, schema)
        attempt = 1
        while True:
            remaining = self.deadline_at - time.perf_counter()
            ctx = CallContext(
                request_id=self.req.request_id, request_t0=self.t0,
                timeout_s=max(0.001, min(self.deps.call_timeout_s, remaining)),
                attempt=attempt, record=self._record,
            )
            try:
                result = await self.deps.provider.complete(request, ctx)
            except ProviderError as err:
                delay = self.deps.retry_policy.retry_delay_s(err, attempt, remaining)
                if delay is None:
                    raise
                await asyncio.sleep(delay)
                attempt += 1
                continue
            try:
                return parse_output(result.content, out)
            except OutputFormatError as exc:
                self.internal[f"{stage}_format_error"] = str(exc)
                kind = "truncated" if result.call.finish_reason == "length" else "malformed"
                raise _Terminal("error", f"{kind}_model_output:{stage}") from exc

    # ------------------------------------------------------------------ stages

    async def _retrieve(self, query: str, attempt: int, top_k: int | None) -> list[Passage]:
        cfg = self.deps.retriever.config(self.setting)
        k = top_k or cfg.top_k
        self.emit(EventKind.RETRIEVAL_START, attempt=attempt, top_k=k)
        # BM25 scoring is CPU work; keep it off the event loop so timers and streams stay live.
        passages = await asyncio.to_thread(self.deps.retriever.search, query, self.setting, k)
        self.internal[f"retrieval_{attempt}"] = {
            "query": query, "top_k": k, "chunk_ids": [p.chunk_id for p in passages],
        }
        self.emit(EventKind.RETRIEVAL_DONE, attempt=attempt, passages=len(passages),
                  chunk_ids=[p.chunk_id for p in passages])
        return passages

    async def _draft(self, question: str, passages: list[Passage], stage: str) -> DraftOut:
        self.emit(EventKind.DRAFT_START, stage=stage, passages=len(passages))
        draft: DraftOut = await self._model(
            stage, draft_messages(question, passages), "draft", DRAFT_SCHEMA, DraftOut, 1200,
        )
        self.internal[stage] = draft.model_dump()
        self.emit(EventKind.DRAFT_DONE, stage=stage, status=draft.status)
        return draft

    def _validate(self, draft: DraftOut, by_id: dict[str, Passage]) -> list[str]:
        problems: list[str] = []
        if not draft.claims:
            problems.append("no_claims")
        if len(draft.claims) > MAX_CLAIMS:
            problems.append("too_many_claims")
        for i, claim in enumerate(draft.claims):
            if not claim.text.strip() or len(claim.text) > MAX_CLAIM_CHARS:
                problems.append(f"claim_{i}:bad_length")
            if not claim.citation_ids:
                problems.append(f"claim_{i}:no_citation")
            for cid in claim.citation_ids:
                passage = by_id.get(cid)
                chunk = self.deps.retriever.get_chunk(self.setting, cid)
                if passage is None:
                    problems.append(f"claim_{i}:not_in_supplied_passages:{cid[:80]}")
                elif (chunk is None or chunk.arxiv_version != passage.arxiv_version
                      or chunk.page != passage.page or chunk.text != passage.text):
                    problems.append(f"claim_{i}:metadata_mismatch:{cid[:80]}")
        return problems

    def _deliver(self, draft: DraftOut, by_id: dict[str, Passage]) -> PipelineResult:
        numbers: dict[str, int] = {}
        for claim in draft.claims:
            for cid in claim.citation_ids:
                numbers.setdefault(cid, len(numbers) + 1)
        pieces = []
        for i, claim in enumerate(draft.claims):
            marks = "".join(f"[{numbers[c]}]" for c in dict.fromkeys(claim.citation_ids))
            piece = f"{claim.text.strip()} {marks}"
            event = self.emit(EventKind.ANSWER_TOKEN, index=i, text=piece,
                              delivery="post_verification")
            if self.first_emit_ms is None:
                self.first_emit_ms = event.ts_ms
            pieces.append(piece)
        citations = [
            Citation(chunk_id=cid, arxiv_version=by_id[cid].arxiv_version,
                     page=by_id[cid].page, passage=by_id[cid].text)
            for cid in numbers
        ]
        claims = [Claim(text=c.text, citation_ids=c.citation_ids) for c in draft.claims]
        return self._result("answered", None, answer=" ".join(pieces), claims=claims,
                            citations=citations)

    async def _body(self) -> PipelineResult:
        question = self.req.question
        self.emit(EventKind.RECEIVED, retrieval_setting=self.setting,
                  source=self.deps.provider.source, model=self.deps.provider.model)
        ledger = self.deps.ledger
        if ledger is not None and not ledger.admit(self.req.request_id, MAX_CALLS_PER_REQUEST):
            # Admission quota rejection: metrics.md classifies this as rate_limited.
            raise _Terminal("rate_limited", "local_call_ceiling_insufficient_for_request")

        self.emit(EventKind.INTERPRET_START)
        interp: InterpretOut = await self._model(
            "interpret", interpret_messages(question), "interpretation", INTERPRET_SCHEMA,
            InterpretOut, 600,
        )
        self.internal["interpret"] = interp.model_dump()
        self.emit(EventKind.INTERPRET_DONE, question_type=interp.question_type)

        passages = await self._retrieve(f"{question} {interp.search_query}", attempt=1, top_k=None)
        draft = await self._draft(question, passages, "draft") if passages else None

        if draft is None or draft.status == "insufficient_evidence":
            # Bounded branch: exactly one expansion with a changed query and a doubled set.
            self.expansion_used = True
            self.emit(EventKind.EXPANSION_START,
                      reason="no_passages" if draft is None else "insufficient_evidence")
            cfg = self.deps.retriever.config(self.setting)
            missing = draft.missing if draft else ""
            passages = await self._retrieve(
                f"{question} {interp.alternate_query} {missing}", attempt=2, top_k=cfg.top_k * 2,
            )
            if not passages:
                return self._result("refused", "no_passages_retrieved")
            draft = await self._draft(question, passages, "draft_expanded")
            if draft.status == "insufficient_evidence":
                return self._result("refused", "insufficient_evidence")

        by_id = {p.chunk_id: p for p in passages}
        problems = self._validate(draft, by_id)
        if problems:
            self.internal["citation_problems"] = problems
            return self._result("refused", "citation_validation_failed")

        cited = {cid: by_id[cid] for c in draft.claims for cid in c.citation_ids}
        self.emit(EventKind.VERIFICATION_START, claims=len(draft.claims))
        verify: VerifyOut = await self._model(
            "verify", verify_messages(draft.claims, cited), "verification", VERIFY_SCHEMA,
            VerifyOut, 600,
        )
        self.internal["verify"] = verify.model_dump()
        if sorted(v.claim_index for v in verify.verdicts) != list(range(len(draft.claims))):
            raise _Terminal("error", "malformed_model_output:verify_coverage")
        # A claim passes only when both fields agree; "supported": true with a reason of
        # partially_supported/contradicted is treated as unsupported.
        unsupported = [v.claim_index for v in verify.verdicts
                       if not (v.supported and v.reason == "supported")]
        if unsupported:
            self.emit(EventKind.VERIFICATION_FAIL, unsupported_claims=len(unsupported),
                      total_claims=len(draft.claims))
            return self._result("refused", "verification_failed")
        self.emit(EventKind.VERIFICATION_PASS, claims=len(draft.claims))
        return self._deliver(draft, cited)

    # ------------------------------------------------------------------ results

    def _result(self, status: TerminalStatus, reason: str | None, **extra: Any) -> PipelineResult:
        sent = [c for c in self.calls if c.sent]
        if status == "refused" and reason:
            message = MESSAGES[reason]
        elif status in ("error", "timeout", "rate_limited", "cancelled"):
            message = OPERATIONAL.format(reason=reason)
        else:
            message = None
        return PipelineResult(
            request_id=self.req.request_id,
            status=status,
            reason=reason,
            message=message,
            delivery="post_verification" if status == "answered" else None,
            source=self.deps.provider.source,
            model=self.deps.provider.model,
            retrieval_setting=self.setting,
            expansion_used=self.expansion_used,
            provider_calls=len(sent),
            upstream_429_count=sum(1 for c in sent if c.status == "rate_limited"),
            tokens_in=sum(c.usage.prompt_tokens or 0 for c in sent),
            tokens_out=sum(c.usage.completion_tokens or 0 for c in sent),
            unknown_usage_calls=sum(1 for c in sent if not c.usage.complete),
            server_first_answer_emit_ms=self.first_emit_ms,
            server_total_ms=self._ms(),
            **extra,
        )

    def _from_provider_error(self, err: ProviderError) -> tuple[TerminalStatus, str]:
        stage = err.call.stage
        if isinstance(err, ProviderRateLimited):
            return "rate_limited", f"upstream_429:{stage}"
        if isinstance(err, ProviderTimeout):
            # The call timeout is clamped to the remaining deadline; when both expire
            # together, report the request deadline regardless of timer ordering.
            if time.perf_counter() >= self.deadline_at - 0.05:
                return "timeout", "request_deadline"
            return "timeout", f"provider_call_timeout:{stage}"
        if isinstance(err, CallCeilingReached):
            return "rate_limited", "local_call_ceiling_reached"
        if isinstance(err, ProviderMalformed):
            if err.call.finish_reason == "length":
                # No answer content before the output-token limit (e.g. all spent on reasoning).
                return "error", f"truncated_model_output:{stage}"
            return "error", f"malformed_upstream_stream:{stage}"
        return "error", f"upstream_error:{stage}"

    def _finish(self, result: PipelineResult, send: bool = True) -> None:
        if self.deps.ledger is not None:
            self.deps.ledger.release(self.req.request_id)
        self.emit(_TERMINAL_KIND[result.status], send=send, **result.model_dump(mode="json"))
        if self.deps.logger:
            self.deps.logger.request({
                **result.model_dump(mode="json"),
                **self.deps.run_info,
                "start_utc": self.start_utc,
                "question": self.req.question,
                "internal": self.internal,
            })

    async def execute(self) -> None:
        try:
            async with asyncio.timeout(self.deps.deadline_s):
                result = await self._body()
        except TimeoutError:
            result = self._result("timeout", "request_deadline")
        except _Terminal as term:
            result = self._result(term.status, term.reason)
        except ProviderError as err:
            result = self._result(*self._from_provider_error(err))
        except asyncio.CancelledError:
            self._finish(self._result("cancelled", "client_disconnected"), send=False)
            raise
        except Exception as exc:  # unexpected defect: still end with a terminal event
            result = self._result("error", f"internal:{type(exc).__name__}")
        self._finish(result)


async def _cancel(fut: asyncio.Future[Any]) -> None:
    fut.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fut


class PipelineRunner:
    def __init__(self, deps: PipelineDeps):
        self.deps = deps

    def validate_setting(self, setting: str) -> None:
        self.deps.retriever.config(setting)

    async def stream(self, req: PipelineRequest) -> AsyncGenerator[PipelineEvent, None]:
        run = _Run(req, self.deps)
        task = asyncio.create_task(run.execute())
        getter: asyncio.Future[PipelineEvent] | None = None
        try:
            while True:
                if not run.queue.empty():
                    event = run.queue.get_nowait()
                elif task.done():
                    return  # task ended without a terminal event (should not happen)
                else:
                    getter = asyncio.ensure_future(run.queue.get())
                    await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                    if not getter.done():
                        await _cancel(getter)
                        getter = None
                        continue  # task finished; drain whatever it queued
                    event = getter.result()
                    getter = None
                yield event
                if event.terminal:
                    return
        finally:
            # Await every cancellation so nothing is left pending (client disconnect path).
            if getter is not None:
                await _cancel(getter)
            if not task.done():
                await _cancel(task)

    async def run(self, req: PipelineRequest) -> tuple[list[PipelineEvent], PipelineResult]:
        events = [event async for event in self.stream(req)]
        return events, PipelineResult.model_validate(events[-1].payload)
