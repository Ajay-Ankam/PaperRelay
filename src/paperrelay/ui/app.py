"""Streamlit UI: an HTTP client of the FastAPI backend (no model calls of its own)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import streamlit as st

API_BASE = os.getenv("PAPERRELAY_API_URL", "http://127.0.0.1:8000")
STAGE_LABELS = {
    "received": "Request received",
    "interpret_start": "Interpreting question",
    "retrieval_start": "Searching the papers",
    "expansion_start": "Not enough evidence yet; one wider search",
    "draft_start": "Drafting from passages",
    "verification_start": "Checking claims against cited passages",
    "verification_pass": "Verified; delivering checked answer",
    "verification_fail": "Verification did not support the draft",
}


def get_config() -> dict[str, Any] | None:
    try:
        resp = httpx.get(f"{API_BASE}/config", timeout=5.0)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
    except httpx.HTTPError:
        return None


st.set_page_config(page_title="PaperRelay", layout="centered")
st.title("PaperRelay")
st.caption("Answers only from three pinned arXiv papers: 1706.03762v7, 2410.00037v1, 2507.07955v2")

config = get_config()
if config is None:
    st.error(f"Backend not reachable at {API_BASE}. Start FastAPI first.")
    st.stop()
assert config is not None
if config["source"] == "real":
    st.info(f"Live provider: Groq `{config['model']}` (run `{config['run_id']}`). "
            f"Real calls used: {config['real_calls_used']} of {config['real_call_ceiling']}.")
else:
    st.warning("OFFLINE TEST DOUBLE: answers are copied sentences chosen by keyword overlap, "
               "not model output. Use only to check the interface.")

question = st.text_area("Question", max_chars=2000)
setting = st.selectbox("Retrieval setting (chunk size in characters)",
                       config["retrieval_settings"],
                       index=config["retrieval_settings"].index(config["retrieval_setting"])
                       if config["retrieval_setting"] in config["retrieval_settings"] else 0)

if st.button("Ask", disabled=not question.strip()):
    progress = st.status("Working...", expanded=True)
    answer_box = st.empty()
    pieces: list[str] = []
    terminal: dict[str, Any] | None = None
    bad_lines = 0
    try:
        with httpx.stream("POST", f"{API_BASE}/ask", timeout=180.0,
                          json={"question": question, "retrieval_setting": setting}) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")[:500]
                st.error(f"Backend rejected the request: HTTP {resp.status_code}")
                st.text(body)
            else:
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        kind = event["kind"]
                    except (ValueError, KeyError, TypeError):
                        bad_lines += 1
                        continue
                    if kind in STAGE_LABELS:
                        progress.write(f"{STAGE_LABELS[kind]} ({event['ts_ms'] / 1000:.1f}s)")
                    elif kind == "answer_token":
                        pieces.append(event["payload"]["text"])
                        # Plain text: answer text derives from untrusted paper content and
                        # model output, so it is never rendered as Markdown/HTML.
                        answer_box.text(" ".join(pieces))
                    elif kind in ("done", "refused", "error", "timeout", "cancelled"):
                        terminal = event["payload"]
    except httpx.HTTPError as exc:
        st.error(f"Connection problem: {type(exc).__name__}")
    if bad_lines:
        st.warning(f"{bad_lines} unreadable stream line(s) were skipped.")

    if terminal is None:
        progress.update(label="Stopped without a result", state="error")
    elif terminal["status"] == "answered":
        progress.update(label="Answered (released after verification)", state="complete",
                        expanded=False)
        st.caption("This text was checked against the cited passages before release. "
                   "Automated checking can still be wrong.")
        for n, cite in enumerate(terminal["citations"], start=1):
            with st.expander(f"[{n}] arXiv {cite['arxiv_version']}, page {cite['page']}"):
                st.text(cite["passage"])
    elif terminal["status"] == "refused":
        progress.update(label="No verified answer", state="complete", expanded=False)
        st.warning(terminal["message"])
    else:
        progress.update(label=f"Failed: {terminal['status']}", state="error", expanded=False)
        st.error(terminal["message"])
    if terminal is not None:
        st.caption(f"request {terminal['request_id']} | source {terminal['source']} | "
                   f"{terminal['provider_calls']} provider calls | server "
                   f"{terminal['server_total_ms'] / 1000:.1f}s")
