"""Analysis tables against hand-computed fixtures (tests/fixtures/experiments/).

burst_run: 6 scheduled, 5 sent (r6 never sent), window 5 s.
  first-token sample (delivered answers r1, r2) = [100, 300]
  total sample (all 5 sent outcomes) = [50, 200, 400, 500, 1000]
  known tokens = 120 + 230 + 160 + 0 + 85 = 595 over 2 delivered answers.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from paperrelay.client.smoke import COLUMNS
from paperrelay.experiments import analyze, limits
from paperrelay.experiments.stats import percentile

FX = Path(__file__).parent / "fixtures" / "experiments"


def test_percentile_matches_numpy_linear_definition():
    xs = [50, 200, 400, 500, 1000]
    assert percentile(xs, 50) == 400
    assert percentile(xs, 95) == pytest.approx(900.0)  # h=3.8 -> 500 + 0.8*500
    assert percentile(xs, 99) == pytest.approx(980.0)
    assert percentile([7], 99) == 7
    assert percentile([], 50) is None  # empty sample is undefined, never 0
    assert percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)


def _row(rows, scenario):
    [r] = [x for x in rows if x["scenario"] == scenario]
    return r


def test_table2_reconciles_with_hand_computed_fixture():
    runs = [analyze.load_run(FX / "burst_run")]
    t2, checks, _ = analyze.scenario_tables(runs)
    r = _row(t2, "burst")
    assert (r["scheduled"], r["sent"], r["never_sent"]) == (6, 5, 1)
    assert r["offered_per_min"] == pytest.approx(60.0)  # 5 sent * 60 / 5 s window
    assert r["first_token"]["n"] == 2 and r["no_answer_token"] == 3
    assert (r["first_token"]["p50"], r["first_token"]["p95"], r["first_token"]["p99"]) == (
        pytest.approx(200.0), pytest.approx(290.0), pytest.approx(298.0))
    assert r["total"]["n"] == 5
    assert (r["total"]["p50"], r["total"]["p95"], r["total"]["p99"]) == (
        pytest.approx(400.0), pytest.approx(900.0), pytest.approx(980.0))
    assert (r["success_pct"], r["rate_limited_pct"], r["timeout_pct"], r["error_pct"]) == (
        60.0, 20.0, 20.0, 0.0)
    assert r["client_abandoned"] == 1
    assert r["retries_per_request"] == pytest.approx(0.6)
    assert r["peak_queue"] == 5
    assert r["tokens_per_answer"] == pytest.approx(297.5)
    assert r["unknown_usage_requests"] == 1
    assert r["cost_per_1000_answers"] is None  # no cost evidence -> not measured
    assert r["successful_total"]["n"] == 3 and r["successful_total"]["p50"] == 400.0
    assert r["scheduling_failures"] == 1 and r["max_arrival_lag_ms"] == 300.0
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_first_token_blanks_are_excluded_not_zero():
    runs = [analyze.load_run(FX / "burst_run")]
    t2, _, _ = analyze.scenario_tables(runs)
    r = _row(t2, "burst")
    # If blanks were read as 0 the sample would contain zeros and p50 would be 0.
    assert r["first_token"]["p50"] == 200.0 and r["first_token"]["n"] == 2


def test_abandoned_and_errored_rows_are_kept_in_totals():
    t2, _, _ = analyze.scenario_tables([analyze.load_run(FX / "burst_run")])
    r = _row(t2, "burst")
    assert r["total"]["n"] == r["sent"] == 5  # abandoned r5 (1000 ms) counted
    assert r["status_counts"]["timeout"] == 1 and r["status_counts"]["rate_limited"] == 1


def test_tokens_per_answer_undefined_with_zero_answers():
    t2, checks, _ = analyze.scenario_tables([analyze.load_run(FX / "zero_answer_run")])
    r = _row(t2, "daily_exhausted")
    assert r["delivered_answers"] == 0
    assert r["tokens_per_answer"] is None and r["known_tokens"] == 300
    assert r["first_token"]["n"] == 0 and r["first_token"]["p50"] is None
    assert r["rate_limited_pct"] == 100.0
    md = analyze.render([], t2, [], checks, {})
    assert "undefined (0 answers)" in md


def test_offered_per_min_uses_window_not_drain_time():
    # Totals reach 1000 ms but the configured window is 5 s: offered = 5*60/5 = 60/min,
    # not anything derived from send-to-last-completion.
    t2, _, _ = analyze.scenario_tables([analyze.load_run(FX / "burst_run")])
    assert _row(t2, "burst")["window_s"] == 5.0
    assert _row(t2, "burst")["offered_per_min"] == 60.0


def test_offline_double_excluded_and_counted():
    runs = [analyze.load_run(FX / "burst_run"), analyze.load_run(FX / "offline_run")]
    t2, _, excluded = analyze.scenario_tables(runs)
    assert excluded == {"offline-double": 1}
    assert {r["source"] for r in t2} == {"stand-in"}


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_source_mixing_in_one_row_is_rejected(tmp_path):
    rows = analyze.read_csv(FX / "burst_run" / "results.csv")
    rows[0]["source"] = "real"  # same run_id + scenario, two sources
    _write(tmp_path / "mixed", rows)
    with pytest.raises(analyze.AnalysisError, match="mix sources"):
        analyze.scenario_tables([analyze.load_run(tmp_path / "mixed")])


def test_duplicate_or_missing_rows_fail_reconciliation(tmp_path):
    rows = analyze.read_csv(FX / "burst_run" / "results.csv")
    _write(tmp_path / "dup", rows + [rows[0]])
    shutil.copy(FX / "burst_run" / "scenario_meta.json", tmp_path / "dup")
    _, checks, _ = analyze.scenario_tables([analyze.load_run(tmp_path / "dup")])
    failed = {c.name.split(": ", 1)[1] for c in checks if not c.ok}
    assert "unique request ids" in failed and "one row per scheduled request" in failed


def test_slow_request_partition_unions_overlaps_and_sums_to_total():
    events = [json.loads(x) for x in
              (FX / "burst_run" / "events.jsonl").read_text().splitlines()]
    b = analyze.breakdown([e for e in events if e["request_id"] == "r5"])
    assert b["server_total_ms"] == 900.0
    # queue [0,300] U rate wait [250,350] = 350 ms (not 400: overlap counted once)
    assert b["total"]["waiting_ms"] == pytest.approx(350.0)
    # retry [320,420] minus waiting -> [350,420] = 70, plus [700,750] = 50
    assert b["total"]["retry_wait_ms"] == pytest.approx(120.0)
    assert b["total"]["working_ms"] == pytest.approx(430.0)
    before, after = b["before_first_token"], b["after_first_token"]
    assert (before["waiting_ms"], before["retry_wait_ms"], before["working_ms"]) == (
        pytest.approx(350.0), pytest.approx(70.0), pytest.approx(180.0))
    assert (after["waiting_ms"], after["retry_wait_ms"], after["working_ms"]) == (
        pytest.approx(0.0), pytest.approx(50.0), pytest.approx(250.0))
    for part in (before, after, b["total"]):
        assert part["waiting_ms"] + part["retry_wait_ms"] + part["working_ms"] == \
            pytest.approx(part["span_ms"])


def test_slow_request_report_picks_slowest_and_shows_client_overhead():
    md, slow, checks = analyze.slow_request_report([analyze.load_run(FX / "burst_run")])
    assert slow[0]["request_id"] == "r5"
    assert slow[0]["client_minus_server_ms"] == pytest.approx(100.0)
    assert all(c.ok for c in checks) and "r5" in md


def test_table3_joins_grades_by_request_id():
    runs = [analyze.load_run(FX / "eval_run")]
    grades = analyze.load_grades([FX / "eval_run" / "grades.csv"])
    t3, checks = analyze.retrieval_table(runs, grades)
    [r] = t3
    assert (r["setting"], r["chunk_size_chars"], r["overlap_chars"], r["top_k"]) == (
        "small", 256, 32, 5)
    assert r["questions"] == 4
    assert (r["correct_cited_pct"], r["wrong_pct"], r["refused_correctly_pct"],
            r["refused_wrongly_pct"]) == (25.0, 25.0, 25.0, 0.0)
    assert r["ungraded"] == 1  # the operational error row stays ungraded
    assert r["tokens_per_answer"] == pytest.approx((1100 + 950 + 200 + 1250) / 2)
    assert r["first_token"]["n"] == 2 and r["first_token"]["p50"] == 1500.0
    assert r["total"]["n"] == 4
    assert all(c.ok for c in checks)


def test_grade_on_operational_failure_fails_check(tmp_path):
    rows = analyze.read_csv(FX / "eval_run" / "grades.csv")
    rows[2]["answer_grade"] = "refused_correctly"  # e3 is an http error, not a refusal
    path = tmp_path / "grades.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    _, checks = analyze.retrieval_table([analyze.load_run(FX / "eval_run")],
                                        analyze.load_grades([path]))
    assert any(not c.ok and "non-ok" in c.name for c in checks)


def test_invalid_grade_value_rejected(tmp_path):
    path = tmp_path / "g.csv"
    path.write_text("request_id,answer_grade\ne1,mostly_right\n", encoding="utf-8")
    with pytest.raises(analyze.AnalysisError, match="invalid grade"):
        analyze.load_grades([path])


def test_limits_prefill_reads_real_headers_only(tmp_path):
    rows = analyze.read_csv(FX / "burst_run" / "results.csv")
    real = dict(rows[0], source="real", run_id="real-x", limit_snapshot_json=json.dumps(
        {"x-ratelimit-limit-requests": "1000", "x-ratelimit-limit-tokens": "8000"}))
    standin = dict(rows[1], limit_snapshot_json=json.dumps(
        {"x-ratelimit-limit-requests": "5", "x-ratelimit-limit-tokens": "5"}))
    _write(tmp_path / "r", [real, standin])
    data = limits.prefill(limits.empty_evidence(), [tmp_path / "r" / "results.csv"])
    items = data["items"]
    assert items["rpd"]["header_advertised"]["value"] == 1000
    assert items["tpm"]["header_advertised"]["value"] == 8000
    assert items["rpm"]["header_advertised"]["value"] is None
    assert items["rpm"]["documented"]["evidence"] == "not measured"
    assert items["concurrency"]["observed"]["value"] is None
    limits.validate(data)


def test_limits_value_without_evidence_rejected():
    data = limits.empty_evidence()
    data["items"]["rpm"]["observed"]["value"] = 30
    with pytest.raises(ValueError, match="without evidence"):
        limits.validate(data)


def test_analyze_end_to_end_writes_reports(tmp_path):
    data, ok = analyze.analyze(
        [FX / "burst_run", FX / "zero_answer_run", FX / "offline_run", FX / "eval_run"],
        [FX / "eval_run" / "grades.csv"], tmp_path / "missing_limits.json", tmp_path / "out")
    assert ok
    md = (tmp_path / "out" / "tables.md").read_text(encoding="utf-8")
    for heading in ("Table 1", "Table 2a", "Table 2b", "Table 3", "Reconciliation checks"):
        assert heading in md
    assert (tmp_path / "out" / "slow_request.md").exists()
    assert json.loads((tmp_path / "out" / "tables.json").read_text())["table2"]
