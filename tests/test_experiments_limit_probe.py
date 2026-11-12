"""Limit probe logic with httpx.MockTransport only (no network, no real key)."""

from __future__ import annotations

import json

import httpx
import pytest

from paperrelay.experiments import limit_probe as lp
from paperrelay.providers.ledger import CallLedger

SSE_OK = ('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
          '"x_groq":{"usage":{"prompt_tokens":5,"completion_tokens":1}}}\n\n'
          "data: [DONE]\n\n")


def headers(remaining_requests: str = "900") -> dict[str, str]:
    return {"x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": remaining_requests,
            "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "7000"}


class Script:
    def __init__(self, statuses: list[int], remaining: str = "900"):
        self.statuses = statuses
        self.remaining = remaining
        self.seen: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append(body)
        assert request.headers["authorization"] == "Bearer test-key"
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 429:
            return httpx.Response(429, headers={**headers(self.remaining), "retry-after": "2"},
                                  json={"error": {"type": "tokens", "message": "slow down"}})
        return httpx.Response(200, headers=headers(self.remaining), content=SSE_OK.encode())


async def run_probe(tmp_path, script, ceiling=10, **cfg_over):
    ledger = CallLedger(tmp_path / "ledger.json", "limit-probe-t", ceiling, hold_lock=False)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    cfg = lp.ProbeConfig(run_id="limit-probe-t", ceiling=ceiling, **cfg_over)
    async with httpx.AsyncClient(transport=httpx.MockTransport(script)) as client:
        summary = await lp.probe(cfg, client, "test-key", ledger, tmp_path / "out",
                                 sleep=fake_sleep)
    calls = [json.loads(x) for x in (tmp_path / "out" / "probe_calls.jsonl").read_text()
             .splitlines()]
    return summary, calls, sleeps, ledger


async def test_stops_at_first_429_then_recovers_honouring_retry_after(tmp_path):
    script = Script([200, 200, 429, 429, 200])
    summary, calls, sleeps, ledger = await run_probe(tmp_path, script, recovery_spacing_s=1.0)
    assert summary["stop"] == "first_429" and summary["first_429"]["n"] == 3
    assert summary["first_429"]["retry_after"] == "2"
    assert summary["recovery"]["recovered"] is True and summary["recovery"]["probes"] == 2
    assert sleeps == [2.0, 2.0]  # max(retry-after 2, spacing 1) before each recovery probe
    assert [c["phase"] for c in calls] == ["burst"] * 3 + ["recovery"] * 2
    assert all(c["rate_limit_headers"]["x-ratelimit-limit-tokens"] == "8000" for c in calls)
    assert ledger.used() == 5
    assert all(b["max_completion_tokens"] == 1 for b in script.seen)


async def test_hard_ceiling_stops_without_network(tmp_path):
    script = Script([])
    summary, calls, _, ledger = await run_probe(tmp_path, script, ceiling=2)
    assert summary["stop"] == "ceiling"
    assert len(script.seen) == 2 and ledger.used() == 2
    assert calls[-1]["status"] == "refused_by_ceiling" and calls[-1]["sent"] is False


async def test_never_pushes_toward_daily_exhaustion(tmp_path):
    script = Script([], remaining="50")
    summary, _, _, _ = await run_probe(tmp_path, script, min_daily_remaining=100)
    assert summary["stop"] == "daily_remaining_below_floor" and len(script.seen) == 1


def test_cli_refuses_without_opt_in_and_bounds(monkeypatch):
    def boom(*a, **k):  # the key must not even be read without the opt-in
        raise AssertionError("key read")

    monkeypatch.setattr(lp, "read_groq_api_key", boom)
    with pytest.raises(SystemExit):
        lp.main(["--run-id", "limit-probe-x", "--ceiling", "5"])
    with pytest.raises(SystemExit):
        lp.main([lp.OPT_IN, "--run-id", "limit-probe-x", "--ceiling", "500"])
    with pytest.raises(SystemExit):
        lp.main([lp.OPT_IN, "--run-id", "other-name", "--ceiling", "5"])


def test_parse_retry_after():
    assert lp.parse_retry_after("2.5") == 2.5
    assert lp.parse_retry_after(None) is None and lp.parse_retry_after("soon") is None
