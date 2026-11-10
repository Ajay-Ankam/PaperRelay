"""Streamlit UI – API health check view for milestone 0."""

import os

import httpx
import streamlit as st

API_BASE = os.getenv("PAPERRELAY_API_URL", "http://localhost:8000")


def check_api_health() -> dict[str, object]:
    try:
        resp = httpx.get(f"{API_BASE}/health", timeout=5.0)
        resp.raise_for_status()
        return {"reachable": True, **resp.json()}
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": str(exc)}


st.set_page_config(page_title="PaperRelay", layout="centered")
st.title("PaperRelay")
st.caption("Research assistant – three pinned arXiv papers")

st.subheader("Backend health")
if st.button("Check", key="health_btn"):
    with st.spinner("Contacting backend…"):
        result = check_api_health()
    if result.get("reachable"):
        st.success(f"Backend OK – version {result.get('version', '?')}")
    else:
        st.error(f"Backend unreachable: {result.get('error', 'unknown')}")
else:
    st.info("Click **Check** to test the backend connection.")
