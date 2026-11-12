"""Deterministic offline test double for development. Not a model and not the stand-in.

It answers by copying one sentence verbatim from the best keyword-matching passage and
"verifies" by substring match. Results are labelled source="offline-double" and must
never be reported as model quality, latency or calibrated stand-in evidence.
"""

from __future__ import annotations

import json
import re
import time

from paperrelay.contracts import Source
from paperrelay.providers.base import CallContext, ChatRequest, ChatResult, make_call

_STOP = frozenset(
    "what which when where does from that this with have were their there they about into "
    "paper papers used uses using between according describe explain".split()
)
_PASSAGE = re.compile(r'<passage id="([^"]+)"[^>]*>(.*?)</passage>', re.DOTALL)
_QUESTION = re.compile(r"<question>(.*?)</question>", re.DOTALL)
_CLAIM = re.compile(r'<claim index="(\d+)" cites="([^"]*)">(.*?)</claim>', re.DOTALL)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _keywords(text: str) -> list[str]:
    seen: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if len(tok) >= 4 and tok not in _STOP and tok not in seen:
            seen.append(tok)
    return seen


def _norm(text: str) -> str:
    return " ".join(text.split())


class OfflineExtractiveProvider:
    name = "offline-extractive"
    source: Source = "offline-double"
    model = "offline-extractive-v1"

    def _interpret(self, user: str) -> dict[str, object]:
        match = _QUESTION.search(user)
        question = match.group(1) if match else user
        return {
            "search_query": _norm(question),
            "alternate_query": " ".join(_keywords(question)),
            "question_type": "other",
        }

    def _draft(self, user: str) -> dict[str, object]:
        match = _QUESTION.search(user)
        keywords = _keywords(match.group(1) if match else "")
        best: tuple[int, str, str] | None = None
        for pid, text in _PASSAGE.findall(user):
            score = sum(1 for k in keywords if k in text.lower())
            if score >= 2 and (best is None or score > best[0]):
                best = (score, pid, text)
        if best is None:
            return {"status": "insufficient_evidence", "claims": [], "missing": "keyword overlap"}
        _, pid, text = best
        for sentence in _SENTENCE.split(_norm(text)):
            if len(sentence) >= 20 and any(k in sentence.lower() for k in keywords):
                claim = {"text": sentence[:400], "citation_ids": [pid]}
                return {"status": "answer", "claims": [claim], "missing": ""}
        return {"status": "insufficient_evidence", "claims": [], "missing": "no usable sentence"}

    def _verify(self, user: str) -> dict[str, object]:
        passages = {pid: _norm(text) for pid, text in _PASSAGE.findall(user)}
        verdicts = []
        for index, cites, text in _CLAIM.findall(user):
            ok = any(_norm(text) in passages.get(c, "") for c in cites.split(",") if c)
            verdicts.append({
                "claim_index": int(index),
                "supported": ok,
                "reason": "supported" if ok else "not_supported",
            })
        return {"verdicts": verdicts}

    def respond(self, kind: str, user: str) -> dict[str, object]:
        handler = {"interpret": self._interpret, "draft": self._draft, "verify": self._verify}
        return handler[kind](user)

    async def complete(self, request: ChatRequest, ctx: CallContext) -> ChatResult:
        t_send = time.perf_counter()
        user = request.messages[-1]["content"]
        handler = {"interpret": self._interpret, "draft": self._draft, "verify": self._verify}
        content = json.dumps(handler[request.stage.removesuffix("_expanded")](user))
        call = make_call(self, request, ctx, status="ok", t_send=t_send, streamed=False)
        ctx.record(call)
        return ChatResult(content, call)


def detect_stage(user: str) -> str:
    """Infer the pipeline stage from prompt structure only (the stand-in is not told the
    stage): claim tags -> verify, passage tags -> draft, otherwise interpret."""
    if _CLAIM.search(user):
        return "verify"
    if _PASSAGE.search(user):
        return "draft"
    return "interpret"
