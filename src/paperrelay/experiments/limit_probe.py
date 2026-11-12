"""Minute-limit probe against the real provider. PREPARED, NOT RUN BY AGENTS.

Spends real quota, so it needs ALL of: --i-understand-this-spends-real-quota, its own
--run-id, a hard --ceiling (<= 60) recorded in evidence/real/call_ledger.json, and Ajay's
explicit main-session authorization. It uses the existing GroqProvider (same HTTP/SSE
code, no hidden retries) wrapped by CeilingGuardProvider + CallLedger, with a tiny prompt
and max_completion_tokens (default 1) to minimise token use.

Phase 1 (burst): sequential calls spaced --interval-s apart until the first HTTP 429, the
ceiling, or --max-burst-calls. Phase 2 (recovery, only after a 429): single calls spaced
by max(retry-after, --recovery-spacing-s) until one succeeds or --recovery-probes is used.
Every call's allow-listed headers are written to evidence/real/<run_id>/probe_calls.jsonl.
Daily exhaustion is never targeted: the probe stops when the advertised remaining daily
requests (x-ratelimit-remaining-requests) fall below --min-daily-remaining.

Note: with max_completion_tokens this small the reasoning model may return no content;
GroqProvider then records the attempt as `malformed`. That still shows the request was
accepted (not 429) and still carries rate-limit headers and usage, which is what the
probe needs; it is not an answer-quality signal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from paperrelay.config import REPO_ROOT, read_groq_api_key
from paperrelay.contracts import ProviderCall
from paperrelay.providers.base import CallContext, ChatRequest, ProviderError
from paperrelay.providers.groq import GroqProvider
from paperrelay.providers.ledger import CallLedger, CeilingGuardProvider

MAX_CEILING = 60
OPT_IN = "--i-understand-this-spends-real-quota"


@dataclass
class ProbeConfig:
    run_id: str
    ceiling: int
    interval_s: float = 0.0
    max_burst_calls: int = 40
    recovery_probes: int = 5
    recovery_spacing_s: float = 5.0
    min_daily_remaining: int = 100
    max_completion_tokens: int = 1
    model: str = "openai/gpt-oss-20b"
    base_url: str = "https://api.groq.com/openai/v1"
    timeout_s: float = 20.0


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _probe_request() -> ChatRequest:
    return ChatRequest(stage="limit_probe", messages=[{"role": "user", "content": "Reply: ok"}],
                       max_completion_tokens=1, schema_name="probe", json_schema={})


async def probe(cfg: ProbeConfig, client: httpx.AsyncClient, api_key: str, ledger: CallLedger,
                out_dir: Path, sleep: Any = asyncio.sleep) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = CeilingGuardProvider(
        GroqProvider(client, api_key, cfg.model, cfg.base_url, "stream_prompt", "low"), ledger)
    request = _probe_request()
    request.max_completion_tokens = cfg.max_completion_tokens
    t0 = time.monotonic()
    log = out_dir / "probe_calls.jsonl"
    calls: list[dict[str, Any]] = []

    async def one(phase: str, n: int) -> ProviderCall:
        recorded: list[ProviderCall] = []
        ctx = CallContext(request_id=f"{cfg.run_id}-{phase}-{n:03d}", request_t0=t0,
                          timeout_s=cfg.timeout_s, record=recorded.append)
        try:
            result = await provider.complete(request, ctx)
            call = result.call
        except ProviderError as exc:
            call = exc.call
        rec = {"phase": phase, "n": n, "utc": datetime.now(UTC).isoformat(),
               **call.model_dump(mode="json")}
        calls.append(rec)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        return call

    def daily_low(call: ProviderCall) -> bool:
        rem = call.rate_limit_headers.get("x-ratelimit-remaining-requests")
        return rem is not None and rem.isdigit() and int(rem) < cfg.min_daily_remaining

    stop = "max_burst_calls"
    first_429: dict[str, Any] | None = None
    for n in range(1, cfg.max_burst_calls + 1):
        call = await one("burst", n)
        if call.status == "refused_by_ceiling":
            stop = "ceiling"
            break
        if call.http_status == 429:
            stop = "first_429"
            first_429 = {"n": n, "send_offset_ms": call.send_offset_ms,
                         "retry_after": call.retry_after, "headers": call.rate_limit_headers}
            break
        if daily_low(call):
            stop = "daily_remaining_below_floor"
            break
        if cfg.interval_s > 0:
            await sleep(cfg.interval_s)
    recovery: dict[str, Any] | None = None
    if stop == "first_429":
        assert first_429 is not None
        t_429 = time.monotonic()
        wait = parse_retry_after(first_429["retry_after"])
        recovery = {"recovered": False, "probes": 0}
        for k in range(1, cfg.recovery_probes + 1):
            await sleep(max(wait or 0.0, cfg.recovery_spacing_s))
            call = await one("recovery", k)
            recovery["probes"] = k
            if call.status == "refused_by_ceiling":
                recovery["stopped"] = "ceiling"
                break
            if call.http_status != 429:
                recovery.update({"recovered": True,
                                 "seconds_after_first_429": round(time.monotonic() - t_429, 3),
                                 "status": call.status})
                break
            wait = parse_retry_after(call.retry_after)
    summary = {"run_id": cfg.run_id, "ceiling": cfg.ceiling, "stop": stop,
               "calls_recorded": len(calls), "first_429": first_429, "recovery": recovery,
               "ledger_used": ledger.used(), "config": vars(cfg),
               "finished_utc": datetime.now(UTC).isoformat()}
    (out_dir / "probe_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Real minute-limit probe (spends quota)")
    parser.add_argument(OPT_IN, dest="opt_in", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ceiling", type=int, required=True)
    parser.add_argument("--interval-s", type=float, default=0.0)
    parser.add_argument("--max-burst-calls", type=int, default=40)
    parser.add_argument("--recovery-probes", type=int, default=5)
    parser.add_argument("--recovery-spacing-s", type=float, default=5.0)
    parser.add_argument("--min-daily-remaining", type=int, default=100)
    parser.add_argument("--max-completion-tokens", type=int, default=1)
    args = parser.parse_args(argv)
    if not args.opt_in:
        parser.error(f"refusing: pass {OPT_IN} (requires Ajay's explicit authorization)")
    if not 0 < args.ceiling <= MAX_CEILING:
        parser.error(f"--ceiling must be within 1..{MAX_CEILING}")
    if not args.run_id.startswith("limit-probe-"):
        parser.error("--run-id must start with 'limit-probe-' (its own ledger entry)")
    out_dir = REPO_ROOT / "evidence" / "real" / args.run_id
    if out_dir.exists():
        parser.error(f"{out_dir} exists; choose a new run id")
    api_key = read_groq_api_key()
    if not api_key:
        raise SystemExit("GROQ_API_KEY not found in environment or .env")
    cfg = ProbeConfig(run_id=args.run_id, ceiling=args.ceiling, interval_s=args.interval_s,
                      max_burst_calls=args.max_burst_calls,
                      recovery_probes=args.recovery_probes,
                      recovery_spacing_s=args.recovery_spacing_s,
                      min_daily_remaining=args.min_daily_remaining,
                      max_completion_tokens=args.max_completion_tokens)
    ledger = CallLedger(REPO_ROOT / "evidence" / "real" / "call_ledger.json", args.run_id,
                        args.ceiling)

    async def go() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s)) as client:
            return await probe(cfg, client, api_key, ledger, out_dir)

    try:
        summary = asyncio.run(go())
    finally:
        ledger.close()
    print(json.dumps({k: summary[k] for k in ("stop", "calls_recorded", "ledger_used")}))


if __name__ == "__main__":
    main()
