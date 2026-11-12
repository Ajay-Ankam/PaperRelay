"""Derive stand-in timing parameters from real call-level evidence.

python -m paperrelay.standin.calibrate --evidence evidence/real/<run_id> [--evidence ...]
    --out standin/calibration-v1-preliminary.json

Uses calls.jsonl only (one record per provider attempt), never pipeline totals. Only
successful streamed calls contribute timing samples. Everything not derived from those
samples is listed under `assumptions`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from paperrelay.config import REPO_ROOT

# The stand-in cannot see the stage name; draft and draft_expanded look alike (passages).
POOLING = {"interpret": ["interpret"], "draft": ["draft", "draft_expanded"],
           "verify": ["verify"]}
FIRST_SIGMA = 0.6
STREAM_SIGMA = 0.5


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _geomean(values: list[float]) -> float:
    positive = [v for v in values if v > 0]
    return math.exp(statistics.fmean(math.log(v) for v in positive)) if positive else 0.0


def _first_call_notes(stages: dict[str, dict[str, list[Any]]]) -> list[str]:
    notes = []
    for stage, s in sorted(stages.items()):
        values = s["provider_first_content_ms"]
        if len(values) >= 2 and values[0] > 3 * statistics.median(values[1:]):
            notes.append(
                f"The first {stage} sample ({values[0]} ms) is more than 3x the others "
                f"({values[1:]}); the cause (connection setup, cold start, provider "
                "variance) was not measured and all samples are kept.")
    return notes


def calibrate(evidence_dirs: list[Path]) -> dict[str, Any]:
    stages: dict[str, dict[str, list[Any]]] = {}
    inputs, run_ids, commits, models = [], [], set(), set()
    limit_headers: dict[str, set[str]] = {}
    excluded = 0
    for d in evidence_dirs:
        calls_path, meta_path = d / "calls.jsonl", d / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("source") != "real":
            raise ValueError(f"{d}: calibration accepts only source=real evidence")
        run_ids.append(meta["run_id"])
        commits.add(meta.get("pipeline_commit", "unknown"))
        inputs += [{"path": _rel(p), "sha256": _sha256(p)} for p in (calls_path, meta_path)]
        for line in calls_path.read_text(encoding="utf-8").splitlines():
            call = json.loads(line)
            models.add(call["model"])
            for name in ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens"):
                if name in call.get("rate_limit_headers", {}):
                    limit_headers.setdefault(name, set()).add(call["rate_limit_headers"][name])
            if call["status"] != "ok" or not call["streamed"] \
                    or call["provider_first_content_ms"] is None:
                excluded += 1
                continue
            s = stages.setdefault(call["stage"], {k: [] for k in (
                "provider_first_content_ms", "total_ms", "stream_ms", "completion_tokens",
                "prompt_tokens", "request_ids")})
            s["provider_first_content_ms"].append(call["provider_first_content_ms"])
            s["total_ms"].append(call["total_ms"])
            s["stream_ms"].append(round(call["total_ms"] - call["provider_first_content_ms"], 3))
            s["completion_tokens"].append(call["usage"]["completion_tokens"])
            s["prompt_tokens"].append(call["usage"]["prompt_tokens"])
            s["request_ids"].append(call["request_id"])

    params: dict[str, Any] = {}
    for target, members in POOLING.items():
        first = [v for m in members for v in stages.get(m, {}).get("provider_first_content_ms", [])]
        stream = [v for m in members for v in stages.get(m, {}).get("stream_ms", [])]
        tokens = [v for m in members for v in stages.get(m, {}).get("completion_tokens", [])
                  if v is not None]
        if not first:
            raise ValueError(f"no successful streamed samples for stage {target!r}")
        params[target] = {
            "pooled_from": members,
            "n": len(first),
            "first_content_median_ms": round(_geomean(first), 1),
            "first_content_sigma": FIRST_SIGMA,
            "stream_median_ms": round(statistics.median(stream), 1),
            "stream_sigma": STREAM_SIGMA,
            "completion_tokens": int(round(statistics.median(tokens))) if tokens else 64,
        }

    return {
        "kind": "paperrelay-standin-calibration",
        "version": "v1-preliminary",
        "preliminary": True,
        "status_note": "Preliminary: derived from very few real calls; timing parameters are "
                       "point estimates with no meaningful confidence interval.",
        "source": "real",
        "source_run_ids": run_ids,
        "pipeline_commits": sorted(commits),
        "models": sorted(models),
        "inputs": inputs,
        "sample_unit": "provider call (calls.jsonl), not pipeline request",
        "sample_counts": {stage: len(v["total_ms"]) for stage, v in sorted(stages.items())},
        "calls_excluded_not_ok_or_not_streamed": excluded,
        "clock_resolution_note": (
            "The real run measured with time.monotonic() on Windows, which ticks at about "
            "15.6 ms; values such as 0.0, 15.0/16.0 and 47.0 ms are quantised to that grid, "
            "so stream durations below ~16 ms are not resolvable. The API now uses "
            "time.perf_counter()."),
        "measured_samples": {stage: {k: v for k, v in s.items()} for stage, s in
                             sorted(stages.items())},
        "header_advertised_limits": {k: sorted(v) for k, v in sorted(limit_headers.items())},
        "standin_parameters": params,
        "estimators": {
            "first_content_median_ms": "geometric mean of pooled samples (lognormal median "
                                       "estimate)",
            "stream_median_ms": "median of (total_ms - provider_first_content_ms)",
            "completion_tokens": "median provider-reported completion tokens (includes "
                                 "hidden reasoning tokens; content is synthetic)",
        },
        "assumptions": [
            "Distribution shape: lognormal per stage; sigma values "
            f"({FIRST_SIGMA} first-content, {STREAM_SIGMA} stream) are synthetic, not "
            "estimated (n <= 3 per stage cannot support a spread estimate).",
            "Pooling: draft and draft_expanded are pooled because the stand-in sees only the "
            "prompt; their real latency may differ with prompt length.",
            *_first_call_notes(stages),
            "Long tail (probability, multiplier) is synthetic: no tail was observed in 6 calls.",
            "Load slowdown with concurrency is synthetic: the real run was sequential (no "
            "concurrent calls measured).",
            "Error, 429 and malformed-stream rates are synthetic: none were observed.",
            "Prompt-token usage reported by the stand-in is chars/4 (synthetic); completion "
            "tokens use the calibrated per-stage median.",
            "Minute/day limit enforcement semantics (sliding 60 s minute window, calendar "
            "simulated-UTC day) are synthetic; only the limit values in "
            "header_advertised_limits were observed.",
            "Real Groq replenishes request/token budgets on a rolling basis (reset headers); "
            "the stand-in day window resets at simulated UTC midnight.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m paperrelay.standin.calibrate")
    parser.add_argument("--evidence", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = calibrate(args.evidence)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: samples {result['sample_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
