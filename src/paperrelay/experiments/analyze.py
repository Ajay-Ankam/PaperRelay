"""Results tables from results.csv (+ grades, limit evidence, event logs).

    python -m paperrelay.experiments.analyze --runs evidence/stand-in/<run> ... \
        [--grades eval/grades_<run>.csv ...] [--limits experiments/limits_evidence.json] \
        --out reports/

Writes reports/tables.md, reports/tables.json and reports/slow_request.md. Exits 1 if a
numerical reconciliation check fails (the files are still written, with the failures).

Denominators (docs/metrics.md, docs/experiments.md):
- sent = rows with an actual_send_time; never-sent rows are counted separately.
- first-token percentiles: delivered answers only (first_token_ms present); n shown,
  sent - n = requests without an answer token. Blanks are excluded, never read as 0.
- total percentiles: every sent request's time-to-terminal (all outcomes).
- outcome %: of sent. tokens/answer: all known tokens of the group (failed and refused
  requests included) / delivered answers; undefined with zero answers.
- offered/min = sent * 60 / offered_window_s (configured window, not drain time).
Sources are never mixed in a row; offline-double rows are excluded from every table.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paperrelay.experiments import limits as limits_mod
from paperrelay.experiments.client import read_jsonl
from paperrelay.experiments.stats import (
    fmt_ms,
    fmt_pct,
    known,
    low_n_flag,
    pct,
    percentile,
    to_float,
)
from paperrelay.retrieval.settings import CONFIGS, get_config

STATUSES = ("ok", "rate_limited", "timeout", "error")
GRADES = ("correct", "wrong", "refused_correctly", "refused_wrongly")
TABLE_SOURCES = ("real", "stand-in")
SCENARIO_ORDER = ("steady", "burst", "sustained_overload", "limit_halved", "daily_exhausted")
TERMINAL_EVENT_KINDS = {"done", "refused", "error", "timeout", "cancelled", "rate_limited"}


class AnalysisError(ValueError):
    pass


@dataclass
class Run:
    path: Path
    rows: list[dict[str, str]]
    scenario_meta: dict[str, Any] = field(default_factory=dict)
    run_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_run(path: Path) -> Run:
    csv_path = path / "results.csv" if path.is_dir() else path
    run_dir = csv_path.parent
    meta_path = run_dir / "scenario_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return Run(path=run_dir, rows=read_csv(csv_path), scenario_meta=meta,
               run_events=read_jsonl(run_dir / "run_events.jsonl"))


def is_sent(row: dict[str, str]) -> bool:
    return bool(row.get("actual_send_time", "").strip())


def has_answer(row: dict[str, str]) -> bool:
    """Delivered answer: an answer token reached the client and the request ended ok.
    (A token followed by abandonment/error is a partial delivery, counted separately.)"""
    return to_float(row.get("first_token_ms")) is not None and row.get("status") == "ok"


def partial_delivery(row: dict[str, str]) -> bool:
    return to_float(row.get("first_token_ms")) is not None and row.get("status") != "ok"


def known_tokens(row: dict[str, str]) -> float:
    return sum(known([row.get("tokens_in"), row.get("tokens_out")]))


def usage_unknown(row: dict[str, str]) -> bool:
    return row.get("tokens_usage_complete", "") != "True"


def _pcts(values: list[float], ps: tuple[float, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(values)}
    for p in ps:
        out[f"p{int(p)}"] = percentile(values, p)
        out[f"p{int(p)}_low_n"] = low_n_flag(p, len(values))
    return out


def _single_source(rows: list[dict[str, str]], label: str) -> str:
    sources = {r.get("source", "") for r in rows}
    if len(sources) != 1:
        raise AnalysisError(f"{label}: rows mix sources {sorted(sources)}; refusing to "
                            f"combine them in one table row")
    return sources.pop()


def _order_key(row: dict[str, Any]) -> tuple[Any, ...]:
    name = row["scenario"]
    idx = SCENARIO_ORDER.index(name) if name in SCENARIO_ORDER else len(SCENARIO_ORDER)
    return (TABLE_SOURCES.index(row["source"]), idx, name, row["run_id"])


# ---------------------------------------------------------------------------
# Table 2a / 2b
# ---------------------------------------------------------------------------

def scenario_tables(runs: list[Run]) -> tuple[list[dict[str, Any]], list[Check],
                                               dict[str, int]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    meta_for: dict[tuple[str, str], dict[str, Any]] = {}
    excluded: dict[str, int] = defaultdict(int)
    for run in runs:
        for csv_row in run.rows:
            if csv_row.get("scenario", "").startswith("eval:"):
                continue
            src = csv_row.get("source", "")
            if src not in TABLE_SOURCES:
                excluded[src or "<blank>"] += 1
                continue
            key = (csv_row.get("run_id", "") or run.path.name, csv_row.get("scenario", ""))
            groups[key].append(csv_row)
            meta_for[key] = run.scenario_meta
    out, checks = [], []
    for (run_id, scenario), rows in groups.items():
        label = f"{run_id}/{scenario}"
        source = _single_source(rows, label)
        sent = [r for r in rows if is_sent(r)]
        never = len(rows) - len(sent)
        windows = {r.get("offered_window_s", "") for r in sent}
        if len(windows) > 1:
            raise AnalysisError(f"{label}: inconsistent offered_window_s {sorted(windows)}")
        window = to_float(next(iter(windows))) if windows else None
        offered = None if not window or window <= 0 else len(sent) * 60.0 / window
        ft = known(r.get("first_token_ms") for r in sent if has_answer(r))
        totals = known(r.get("total_ms") for r in sent)
        counts = {s: sum(1 for r in sent if r.get("status") == s) for s in STATUSES}
        other = len(sent) - sum(counts.values())
        delivered = sum(1 for r in sent if has_answer(r))
        tokens = sum(known_tokens(r) for r in sent)
        retries_known = [to_float(r.get("retries")) for r in sent]
        retries_vals = [v for v in retries_known if v is not None]
        peaks = known(r.get("queue_peak_observed") for r in sent)
        lags = known(r.get("arrival_lag_ms") for r in sent)
        threshold = to_float(meta_for[(run_id, scenario)].get("lag_threshold_ms"))
        costs = [to_float(r.get("cost_usd")) for r in sent]
        cost_known = bool(sent) and all(c is not None for c in costs) and delivered > 0
        ok_rows = [r for r in sent if r.get("status") == "ok"]
        row: dict[str, Any] = {
            "source": source, "scenario": scenario, "run_id": run_id,
            "scheduled": len(rows), "sent": len(sent), "never_sent": never,
            "window_s": window, "offered_per_min": offered,
            "first_token": _pcts(ft, (50, 95, 99)),
            "no_answer_token": len(sent) - len(ft),
            "partial_deliveries": sum(1 for r in sent if partial_delivery(r)),
            "total": _pcts(totals, (50, 95, 99)),
            "status_counts": {**counts, "unrecognised": other},
            "success_pct": pct(counts["ok"], len(sent)),
            "rate_limited_pct": pct(counts["rate_limited"], len(sent)),
            "timeout_pct": pct(counts["timeout"], len(sent)),
            "error_pct": pct(counts["error"], len(sent)),
            "client_abandoned": sum(1 for r in sent
                                    if r.get("outcome_reason") == "client_abandoned"),
            "retries_per_request": (sum(retries_vals) / len(sent)
                                    if sent and len(retries_vals) == len(sent) else None),
            "retries_unknown": len(sent) - len(retries_vals),
            "peak_queue": max(peaks) if peaks else None,
            "delivered_answers": delivered,
            "known_tokens": tokens,
            "tokens_per_answer": tokens / delivered if delivered else None,
            "unknown_usage_requests": sum(1 for r in sent if usage_unknown(r)),
            "cost_per_1000_answers": (1000 * sum(c for c in costs if c is not None) / delivered
                                      if cost_known else None),
            "successful_total": _pcts(known(r.get("total_ms") for r in ok_rows), (50, 95, 99)),
            "max_arrival_lag_ms": max(lags) if lags else None,
            "scheduling_failures": (sum(1 for v in lags if v > threshold)
                                    if threshold is not None else None),
            "lag_threshold_ms": threshold,
        }
        out.append(row)
        checks += _scenario_checks(label, row, rows, sent, meta_for[(run_id, scenario)])
    out.sort(key=_order_key)
    return out, checks, dict(excluded)


def _scenario_checks(label: str, row: dict[str, Any], rows: list[dict[str, str]],
                     sent: list[dict[str, str]], meta: dict[str, Any]) -> list[Check]:
    checks = []
    ids = [r.get("request_id") for r in rows]
    checks.append(Check(f"{label}: unique request ids", len(ids) == len(set(ids)),
                        f"{len(ids)} rows, {len(set(ids))} distinct"))
    if meta.get("requests") is not None:
        checks.append(Check(f"{label}: one row per scheduled request",
                            len(rows) == int(meta["requests"]),
                            f"rows={len(rows)} scheduled={meta['requests']}"))
    sc = row["status_counts"]
    checks.append(Check(f"{label}: status counts sum to sent",
                        sum(sc.values()) == len(sent) and sc["unrecognised"] == 0,
                        f"{sc} vs sent={len(sent)}"))
    if sent:
        total_pct = sum(row[k] for k in ("success_pct", "rate_limited_pct", "timeout_pct",
                                         "error_pct"))
        checks.append(Check(f"{label}: outcome percentages sum to 100",
                            abs(total_pct - 100.0) < 1e-9, f"sum={total_pct:.6f}"))
    checks.append(Check(f"{label}: first-token n + no-token = sent",
                        row["first_token"]["n"] + row["no_answer_token"] == len(sent),
                        f"{row['first_token']['n']} + {row['no_answer_token']} vs {len(sent)}"))
    checks.append(Check(f"{label}: every sent request has a total time",
                        row["total"]["n"] == len(sent),
                        f"total n={row['total']['n']} sent={len(sent)}"))
    checks.append(Check(f"{label}: delivered answers = first-token sample",
                        row["delivered_answers"] == row["first_token"]["n"],
                        f"{row['delivered_answers']} vs n={row['first_token']['n']}"))
    if row["window_s"]:
        checks.append(Check(f"{label}: offered/min uses the configured window",
                            abs(row["offered_per_min"] - len(sent) * 60 / row["window_s"])
                            < 1e-9, f"window_s={row['window_s']}"))
    return checks


# ---------------------------------------------------------------------------
# Table 3 (retrieval quality)
# ---------------------------------------------------------------------------

def load_grades(paths: list[Path]) -> dict[str, dict[str, str]]:
    grades: dict[str, dict[str, str]] = {}
    for path in paths:
        for row in read_csv(path):
            rid = row.get("request_id", "")
            grade = (row.get("answer_grade") or "").strip()
            if grade and grade not in GRADES:
                raise AnalysisError(f"{path}: invalid grade {grade!r} for {rid}")
            if rid in grades and grades[rid].get("answer_grade", "") != grade:
                raise AnalysisError(f"conflicting grades for request {rid}")
            grades[rid] = {**row, "answer_grade": grade}
    return grades


def retrieval_table(runs: list[Run], grades: dict[str, dict[str, str]]
                    ) -> tuple[list[dict[str, Any]], list[Check]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for run in runs:
        for csv_row in run.rows:
            if not csv_row.get("scenario", "").startswith("eval:"):
                continue
            if csv_row.get("source") not in TABLE_SOURCES:
                continue
            groups[(csv_row["source"], csv_row.get("retrieval_setting", ""))].append(csv_row)
    out, checks = [], []
    for (source, setting), rows in sorted(groups.items()):
        label = f"eval/{source}/{setting}"
        _single_source(rows, label)
        n = len(rows)
        counts = {g: 0 for g in GRADES}
        ungraded = 0
        bad = 0
        for r in rows:
            g = grades.get(r["request_id"], {}).get("answer_grade", "") or r.get(
                "answer_grade", "")
            if g and r.get("status") != "ok":
                bad += 1  # an operational failure must never carry an answer grade
            if g in counts:
                counts[g] += 1
            else:
                ungraded += 1
        delivered = sum(1 for r in rows if has_answer(r))
        tokens = sum(known_tokens(r) for r in rows)
        chunk: int | None = None
        overlap: int | None = None
        top_k: int | None = None
        try:
            cfg = get_config(setting)
            chunk, overlap, top_k = cfg.chunk_size, cfg.overlap, cfg.top_k
        except KeyError:
            pass
        row: dict[str, Any] = {
            "source": source, "setting": setting, "chunk_size_chars": chunk,
            "overlap_chars": overlap, "top_k": top_k, "rerank": "none",
            "questions": n, "distinct_question_ids": len({r.get("question_id") for r in rows}),
            "correct_cited_pct": pct(counts["correct"], n), "wrong_pct": pct(counts["wrong"], n),
            "refused_correctly_pct": pct(counts["refused_correctly"], n),
            "refused_wrongly_pct": pct(counts["refused_wrongly"], n),
            "ungraded": ungraded, "grade_counts": counts,
            "delivered_answers": delivered, "known_tokens": tokens,
            "tokens_per_answer": tokens / delivered if delivered else None,
            "unknown_usage_requests": sum(1 for r in rows if usage_unknown(r)),
            "first_token": _pcts(known(r.get("first_token_ms") for r in rows if has_answer(r)),
                                 (50, 95)),
            "total": _pcts(known(r.get("total_ms") for r in rows), (50, 95)),
        }
        out.append(row)
        checks.append(Check(f"{label}: grades + ungraded = questions",
                            sum(counts.values()) + ungraded == n,
                            f"{counts} + ungraded {ungraded} vs {n}"))
        checks.append(Check(f"{label}: no grade on a non-ok (operational failure) row",
                            bad == 0, f"{bad} graded non-ok rows"))
        checks.append(Check(f"{label}: one row per question",
                            row["distinct_question_ids"] == n,
                            f"{row['distinct_question_ids']} distinct of {n}"))
    return out, checks


# ---------------------------------------------------------------------------
# Slow-request breakdown (events.jsonl intervals, union, no double counting)
# ---------------------------------------------------------------------------

Interval = tuple[float, float]


def union(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def measure(intervals: list[Interval], lo: float, hi: float) -> float:
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in intervals)


def subtract(intervals: list[Interval], minus: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in intervals:
        pieces = [(a, b)]
        for c, d in minus:
            nxt = []
            for x, y in pieces:
                if d <= x or c >= y:
                    nxt.append((x, y))
                else:
                    if c > x:
                        nxt.append((x, c))
                    if d < y:
                        nxt.append((d, y))
            pieces = nxt
        out += pieces
    return union(out)


def breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Partition one request's server timeline (ms from server request start).

    Waiting = admission queue + minute-budget rate waits; retry wait = backoff sleeps not
    already inside waiting; working = everything else. Assumptions (documented): a
    `rate_wait` / `retry_wait` event is emitted when the wait starts and covers
    [ts, ts + wait_ms|delay_ms]; queue wait ends at `admitted` (payload queue_wait_ms) or
    spans `queued` -> admitted/terminal. Overlaps are unioned; waiting takes precedence.
    """
    evs = sorted(events, key=lambda e: float(e.get("ts_ms", 0.0)))
    terminal = next((e for e in evs if e.get("kind") in TERMINAL_EVENT_KINDS), None)
    end = float(terminal["ts_ms"]) if terminal else max(
        (float(e.get("ts_ms", 0.0)) for e in evs), default=0.0)
    waiting: list[Interval] = []
    retry: list[Interval] = []
    queued = next((e for e in evs if e.get("kind") == "queued"), None)
    admitted = next((e for e in evs if e.get("kind") == "admitted"), None)
    if admitted is not None and to_float((admitted.get("payload") or {}).get("queue_wait_ms")) \
            is not None:
        ts = float(admitted["ts_ms"])
        waiting.append((ts - float(admitted["payload"]["queue_wait_ms"]), ts))
    elif queued is not None:
        stop = float(admitted["ts_ms"]) if admitted is not None else end
        waiting.append((float(queued["ts_ms"]), stop))
    for e in evs:
        payload = e.get("payload") or {}
        ts = float(e.get("ts_ms", 0.0))
        if e.get("kind") == "rate_wait":
            dur = to_float(payload.get("wait_ms"))
            if dur is not None:
                waiting.append((ts, ts + dur))
        elif e.get("kind") == "retry_wait":
            dur = to_float(payload.get("delay_ms"))
            if dur is not None:
                retry.append((ts, ts + dur))
    clip = [(max(0.0, a), min(end, b)) for a, b in waiting]
    w = union(clip)
    r = subtract(union([(max(0.0, a), min(end, b)) for a, b in retry]), w)
    first = next((float(e["ts_ms"]) for e in evs if e.get("kind") == "answer_token"), None)
    split = first if first is not None else end

    def part(lo: float, hi: float) -> dict[str, float]:
        wait_ms = measure(w, lo, hi)
        retry_ms = measure(r, lo, hi)
        return {"waiting_ms": wait_ms, "retry_wait_ms": retry_ms,
                "working_ms": (hi - lo) - wait_ms - retry_ms, "span_ms": hi - lo}

    return {"server_total_ms": end, "server_first_answer_ms": first,
            "before_first_token": part(0.0, split), "after_first_token": part(split, end),
            "total": part(0.0, end), "waiting_intervals": w, "retry_intervals": r}


def slow_request_report(runs: list[Run]) -> tuple[str, list[dict[str, Any]], list[Check]]:
    lines = ["# Slowest request per run: waiting / working / retry wait", "",
             "Server timeline from events.jsonl (ms from server request start). Waiting = "
             "admission queue + minute rate waits; retry wait = backoff sleeps; working = the "
             "rest. Intervals are unioned (overlaps counted once). The client-observed total "
             "also includes network/streaming/client time, shown separately.", ""]
    out, checks = [], []
    for run in runs:
        sent = [r for r in run.rows if is_sent(r) and r.get("source") in TABLE_SOURCES
                and to_float(r.get("total_ms")) is not None]
        if not sent:
            continue
        slow = max(sent, key=lambda r: to_float(r["total_ms"]) or 0.0)
        events = [e for e in read_jsonl(run.path / "events.jsonl")
                  if e.get("request_id") == slow["request_id"]]
        lines += [f"## {slow.get('run_id')} / {slow.get('scenario')} ({slow.get('source')})",
                  f"request_id `{slow['request_id']}`, status {slow['status']}, reason "
                  f"{slow.get('outcome_reason') or '-'}, client total {slow['total_ms']} ms, "
                  f"client first token {slow.get('first_token_ms') or 'none'} ms.", ""]
        if not events:
            lines += ["No server events recorded for this request (not measured).", ""]
            continue
        b = breakdown(events)
        client_total = to_float(slow["total_ms"]) or 0.0
        b["client_total_ms"] = client_total
        b["client_minus_server_ms"] = client_total - b["server_total_ms"]
        b["request_id"], b["run_id"] = slow["request_id"], slow.get("run_id")
        out.append(b)
        t = b["total"]
        checks.append(Check(f"{slow.get('run_id')}: slow-request partition sums to server "
                            f"total", abs(t["waiting_ms"] + t["retry_wait_ms"] + t["working_ms"]
                                          - b["server_total_ms"]) < 1e-6,
                            f"{t} vs {b['server_total_ms']}"))
        split_note = ("server first answer token at "
                      f"{b['server_first_answer_ms']:.1f} ms" if b["server_first_answer_ms"]
                      is not None else "no answer token: everything is before first token")
        lines += [f"Split at {split_note}.", "",
                  "| Component | Before first token (ms) | After first token (ms) | Total (ms) |",
                  "|---|---|---|---|"]
        for key, name in (("waiting_ms", "Waiting (queue + rate wait)"),
                          ("retry_wait_ms", "Retry wait"), ("working_ms", "Working (server)")):
            lines.append(f"| {name} | {b['before_first_token'][key]:.1f} | "
                         f"{b['after_first_token'][key]:.1f} | {t[key]:.1f} |")
        lines += [f"| Server total | {b['before_first_token']['span_ms']:.1f} | "
                  f"{b['after_first_token']['span_ms']:.1f} | {b['server_total_ms']:.1f} |",
                  f"| Client total minus server total (network/stream/client) | | | "
                  f"{b['client_minus_server_ms']:.1f} |",
                  f"| Client-observed total | | | {client_total:.1f} |", ""]
    return "\n".join(lines) + "\n", out, checks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _p(block: dict[str, Any], p: int) -> str:
    v = block.get(f"p{p}")
    s = fmt_ms(v)
    return s + ("*" if v is not None and block.get(f"p{p}_low_n") else "")


def _num(v: Any, digits: int = 1, none: str = "n/a") -> str:
    return none if v is None else f"{v:.{digits}f}"


def render(t1: list[dict[str, Any]], t2: list[dict[str, Any]], t3: list[dict[str, Any]],
           checks: list[Check], excluded: dict[str, int]) -> str:
    L = ["# Results tables", "",
         f"Generated {datetime.now(UTC).isoformat()} by paperrelay.experiments.analyze. "
         "Percentiles: linear interpolation (numpy default); `*` = sample below 20 (p95) or "
         "100 (p99): interpolated, not reliable. Real and stand-in rows are never combined.",
         ""]
    if excluded:
        L += [f"Excluded rows by source (never tabulated): {excluded}", ""]
    L += ["## Table 1. Provider limits", "",
          "| Limit | Kind | Value | Evidence | Confidence |", "|---|---|---|---|---|"]
    for r in t1:
        L.append(f"| {r['label']} | {r['kind']} | {r['value']} | {r['evidence']} | "
                 f"{r['confidence']} |")
    L += ["", "## Table 2a. Latency per scenario", "",
          "| Source | Scenario | Run | Sent | Arrival window (s) | Offered/min | "
          "First token p50/p95/p99 ms (n) | Total p50/p95/p99 ms (n) |",
          "|---|---|---|---|---|---|---|---|"]
    for r in t2:
        ft, tt = r["first_token"], r["total"]
        L.append(
            f"| {r['source']} | {r['scenario']} | {r['run_id']} | {r['sent']} | "
            f"{_num(r['window_s'], 1, 'not measured')} | "
            f"{_num(r['offered_per_min'], 1, 'not measured')} | "
            f"{_p(ft, 50)} / {_p(ft, 95)} / {_p(ft, 99)} (n={ft['n']}; no delivered answer: "
            f"{r['no_answer_token']}) | {_p(tt, 50)} / {_p(tt, 95)} / {_p(tt, 99)} "
            f"(n={tt['n']}) |")
    L += ["", "## Table 2b. Outcomes and cost per scenario", "",
          "Percentages of sent requests. Success = transport status ok (answers and "
          "refusals). Tokens/answer = all known tokens incl. failed attempts / delivered "
          "answers.", "",
          "| Source | Scenario | Success % | Rate-limited % | Timeout/given-up % | Error % | "
          "Retries/request | Peak queue | Tokens/answer | Cost/1000 answers |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in t2:
        tpa = ("undefined (0 answers)" if r["tokens_per_answer"] is None
               else f"{r['tokens_per_answer']:.1f} ({r['unknown_usage_requests']} unknown-usage)")
        L.append(
            f"| {r['source']} | {r['scenario']} | {fmt_pct(r['success_pct'])} | "
            f"{fmt_pct(r['rate_limited_pct'])} | {fmt_pct(r['timeout_pct'])} | "
            f"{fmt_pct(r['error_pct'])} | "
            f"{_num(r['retries_per_request'], 2, 'not measured')} | "
            f"{_num(r['peak_queue'], 0, 'not measured')} | {tpa} | "
            f"{_num(r['cost_per_1000_answers'], 4, 'not measured')} |")
    L += ["", "### Supplemental (same rows)", "",
          "| Source | Scenario | Scheduled | Never sent | Client abandoned | Delivered answers "
          "| Partial deliveries | Successful-only total p50/p95/p99 ms (n) | Max arrival lag ms "
          "| Scheduling failures |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in t2:
        st = r["successful_total"]
        L.append(
            f"| {r['source']} | {r['scenario']} | {r['scheduled']} | {r['never_sent']} | "
            f"{r['client_abandoned']} | {r['delivered_answers']} | "
            f"{r['partial_deliveries']} | {_p(st, 50)} / "
            f"{_p(st, 95)} / {_p(st, 99)} (n={st['n']}) | "
            f"{_num(r['max_arrival_lag_ms'], 1, 'not measured')} | "
            f"{'not measured' if r['scheduling_failures'] is None else r['scheduling_failures']}"
            f" |")
    L += ["", "## Table 3. Retrieval quality versus cost", "",
          "Chunk size/overlap in characters. Percentages divide by questions attempted; "
          "ungraded = no assessable answer or not yet graded.", "",
          "| Source | Setting (chunk/overlap chars, top_k, rerank) | Questions | Correct+cited % | "
          "Wrong % | Correctly refused % | Refused wrongly % | Ungraded | Tokens/answer | "
          "First token p50/p95 ms (n) | Total p50/p95 ms (n) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    if not t3:
        L.append("| not measured | no graded evaluation runs supplied | | | | | | | | | |")
    for r in t3:
        tpa = ("undefined (0 answers)" if r["tokens_per_answer"] is None
               else f"{r['tokens_per_answer']:.1f}")
        L.append(
            f"| {r['source']} | {r['setting']} ({r['chunk_size_chars']}/{r['overlap_chars']}, "
            f"k={r['top_k']}, {r['rerank']}) | {r['questions']} | "
            f"{fmt_pct(r['correct_cited_pct'])} | {fmt_pct(r['wrong_pct'])} | "
            f"{fmt_pct(r['refused_correctly_pct'])} | {fmt_pct(r['refused_wrongly_pct'])} | "
            f"{r['ungraded']} | {tpa} | {_p(r['first_token'], 50)} / {_p(r['first_token'], 95)} "
            f"(n={r['first_token']['n']}) | {_p(r['total'], 50)} / {_p(r['total'], 95)} "
            f"(n={r['total']['n']}) |")
    failed = [c for c in checks if not c.ok]
    L += ["", "## Reconciliation checks", "",
          f"{len(checks) - len(failed)} of {len(checks)} passed.", ""]
    for c in checks:
        L.append(f"- [{'x' if c.ok else ' '}] {c.name}: {c.detail}")
    return "\n".join(L) + "\n"


def analyze(run_paths: list[Path], grade_paths: list[Path], limits_path: Path,
            out_dir: Path) -> tuple[dict[str, Any], bool]:
    runs = [load_run(p) for p in run_paths]
    t1 = limits_mod.table_rows(limits_mod.load(limits_path))
    t2, checks, excluded = scenario_tables(runs)
    t3, checks3 = retrieval_table(runs, load_grades(grade_paths))
    slow_md, slow, checks_slow = slow_request_report(runs)
    checks = checks + checks3 + checks_slow
    data = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "inputs": {"runs": [str(p) for p in run_paths], "grades": [str(p) for p in grade_paths],
                   "limits": str(limits_path)},
        "percentile_method": "linear interpolation between closest ranks (numpy default)",
        "excluded_rows_by_source": excluded,
        "retrieval_settings": {k: vars(v) for k, v in CONFIGS.items()},
        "table1": t1, "table2": t2, "table3": t3, "slow_requests": slow,
        "checks": [c.as_dict() for c in checks],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tables.json").write_text(json.dumps(data, indent=2, default=str),
                                         encoding="utf-8")
    (out_dir / "tables.md").write_text(render(t1, t2, t3, checks, excluded), encoding="utf-8")
    (out_dir / "slow_request.md").write_text(slow_md, encoding="utf-8")
    return data, all(c.ok for c in checks)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build results tables")
    parser.add_argument("--runs", nargs="+", required=True,
                        help="run directories (or results.csv files)")
    parser.add_argument("--grades", nargs="*", default=[])
    parser.add_argument("--limits", default=str(limits_mod.DEFAULT_PATH))
    parser.add_argument("--out", default="reports")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    try:
        _, ok = analyze([Path(p) for p in args.runs], [Path(p) for p in args.grades],
                        Path(args.limits), Path(args.out))
    except AnalysisError as exc:
        raise SystemExit(f"analysis refused: {exc}") from exc
    print(f"wrote {args.out}/tables.md, tables.json, slow_request.md; checks "
          f"{'passed' if ok else 'FAILED'}")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
