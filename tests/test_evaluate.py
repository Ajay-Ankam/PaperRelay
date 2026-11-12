"""Evaluation runner: budget stops, ledger guard, resume and grade-sheet join (fake API)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperrelay.experiments import evaluate as ev

HEADER = ev.QUESTION_COLUMNS


def write_questions(path: Path, n: int = 4, author: str = "Ajay") -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for i in range(1, n + 1):
            w.writerow({"question_id": f"q{i}", "question": f"Test question {i}?",
                        "category": "answerable", "expected_answerable": "yes",
                        "reference_paper": "1706.03762v7", "reference_page": "5",
                        "reference_passage": "ref", "author": author})
    return path


class FakeAPI:
    """/config + /ask. Each /ask uses 3 provider calls; q3 always fails with an error."""

    def __init__(self, run_dir: Path, source: str = "stand-in", ceiling: int | None = None):
        self.run_dir = run_dir
        self.source = source
        self.ceiling = ceiling
        self.used = 0
        self.asked: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/config":
            return httpx.Response(200, json={
                "source": self.source, "model": "m", "run_id": "eval-run",
                "evidence_dir": str(self.run_dir),
                "retrieval_settings": ["small", "medium", "large"],
                "real_call_ceiling": self.ceiling,
                "real_calls_used": self.used if self.ceiling else None,
                "max_calls_per_request": 6})
        body = json.loads(request.content)
        self.asked.append(body)
        self.used += 3
        rid = body["request_id"]
        base = {"request_id": rid, "source": self.source, "model": "m",
                "retrieval_setting": body["retrieval_setting"], "provider_calls": 3,
                "unknown_usage_calls": 0, "tokens_in": 100, "tokens_out": 10}
        if "3" in body["question"]:
            lines = [{"kind": "error", "ts_ms": 1,
                      "payload": {**base, "status": "error", "reason": "http_error"}}]
        else:
            lines = [{"kind": "answer_token", "ts_ms": 1, "payload": {"text": "A."}},
                     {"kind": "done", "ts_ms": 2, "payload": {
                         **base, "status": "answered", "answer": "A. [1]",
                         "citations": [{"chunk_id": "c1", "page": 5, "passage": "p",
                                        "arxiv_version": "v"}]}}]
        return httpx.Response(200, content="".join(json.dumps(x) + "\n" for x in lines))


def args_for(tmp_path: Path, questions: Path, **over: Any):
    argv = ["--questions", str(questions), "--settings", "small,medium", "--run-id", "eval-run",
            "--api", "http://t", "--expected-source", "stand-in", "--max-calls", "100",
            "--max-tokens", "1000000", "--grades-dir", str(tmp_path / "eval")]
    args = ev.build_parser().parse_args(argv)
    for k, v in over.items():
        setattr(args, k, v)
    return args


def results(run_dir: Path) -> list[dict[str, str]]:
    return ev.read_results(run_dir / "results.csv")


async def test_stops_before_request_when_call_budget_would_be_exceeded(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    api = FakeAPI(tmp_path / "run")
    (tmp_path / "run").mkdir()
    # worst case 6/request: 0+6, 3+6, 6+6 <= 12 allowed; 9+6 > 12 stops.
    summary = await ev.evaluate(args_for(tmp_path, q, max_calls=12),
                                transport=httpx.MockTransport(api.handler))
    assert summary["sent"] == 3 and "max-calls" in summary["stopped"]
    assert len(results(tmp_path / "run")) == 3


async def test_token_budget_uses_worst_case_estimate(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    (tmp_path / "run").mkdir()
    api = FakeAPI(tmp_path / "run")
    summary = await ev.evaluate(
        args_for(tmp_path, q, max_tokens=2000, worst_case_tokens_per_request=1500),
        transport=httpx.MockTransport(api.handler))
    # 0 + 1500 <= 2000 sends; 110 + 1500 <= 2000 sends; 220 + 1500 sends; 330+1500 sends;
    # 440 + 1500 sends; 550 + 1500 > 2000 stops.
    assert summary["sent"] == 5 and "max-tokens" in summary["stopped"]


async def test_resume_skips_done_pairs_and_recharges_spent_budget(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    (tmp_path / "run").mkdir()
    api = FakeAPI(tmp_path / "run")
    first = await ev.evaluate(args_for(tmp_path, q, max_calls=12),
                              transport=httpx.MockTransport(api.handler))
    assert first["sent"] == 3
    again = await ev.evaluate(args_for(tmp_path, q, max_calls=12),
                              transport=httpx.MockTransport(api.handler))
    assert again["sent"] == 0  # earlier spend (no server records -> worst case) is charged
    rest = await ev.evaluate(args_for(tmp_path, q, max_calls=1000),
                             transport=httpx.MockTransport(api.handler))
    assert rest["sent"] == 5
    rows = results(tmp_path / "run")
    pairs = [(r["question_id"], r["retrieval_setting"]) for r in rows]
    assert len(pairs) == 8 == len(set(pairs))
    assert {r["scenario"] for r in rows} == {"eval:small", "eval:medium"}


async def test_real_source_requires_ceiling_and_stops_on_ledger_remaining(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    (tmp_path / "run").mkdir()
    no_ceiling = FakeAPI(tmp_path / "run", source="real", ceiling=None)
    with pytest.raises(ev.EvalError, match="ceiling"):
        await ev.evaluate(args_for(tmp_path, q, expected_source="real"),
                          transport=httpx.MockTransport(no_ceiling.handler))
    api = FakeAPI(tmp_path / "run", source="real", ceiling=10)
    summary = await ev.evaluate(args_for(tmp_path, q, expected_source="real"),
                                transport=httpx.MockTransport(api.handler))
    # remaining 10 >= 6 sends; 7 >= 6 sends; 4 < 6 stops.
    assert summary["sent"] == 2 and "ledger_remaining 4" in summary["stopped"]


async def test_refuses_wrong_source_wrong_run_and_real_dry_run(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    (tmp_path / "run").mkdir()
    api = FakeAPI(tmp_path / "run", source="offline-double")
    with pytest.raises(ev.EvalError, match="source"):
        await ev.evaluate(args_for(tmp_path, q), transport=httpx.MockTransport(api.handler))
    api2 = FakeAPI(tmp_path / "run")
    with pytest.raises(ev.EvalError, match="run_id"):
        await ev.evaluate(args_for(tmp_path, q, run_id="other"),
                          transport=httpx.MockTransport(api2.handler))
    real = FakeAPI(tmp_path / "run", source="real", ceiling=50)
    with pytest.raises(ev.EvalError, match="dry-run"):
        await ev.evaluate(args_for(tmp_path, q, expected_source="real", dry_run=True),
                          transport=httpx.MockTransport(real.handler))
    assert api.asked == api2.asked == real.asked == []


async def test_grade_sheet_marks_failures_not_gradable_and_preserves_grades(tmp_path):
    q = write_questions(tmp_path / "q.csv")
    (tmp_path / "run").mkdir()
    api = FakeAPI(tmp_path / "run")
    s = await ev.evaluate(args_for(tmp_path, q, settings="small"),
                          transport=httpx.MockTransport(api.handler))
    path = Path(s["grades_path"])
    assert path == tmp_path / "eval" / "grades_eval-run.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ev.GRADE_COLUMNS
    by_q = {r["question_id"]: r for r in rows}
    assert by_q["q3"]["gradable"].startswith("no (operational")
    assert all(r["answer_grade"] == "" for r in rows)  # never pre-graded
    assert by_q["q1"]["answer"] == "A. [1]" and "c1 (p.5)" in by_q["q1"]["citations"]
    assert by_q["q1"]["reference_paper"] == "1706.03762v7"
    # Ajay grades q1; a stray grade on the failed q3 must not survive a rebuild.
    by_q["q1"]["answer_grade"], by_q["q1"]["notes"] = "correct", "checked p5"
    by_q["q3"]["answer_grade"] = "wrong"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ev.GRADE_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    await ev.evaluate(args_for(tmp_path, q, settings="small"),
                      transport=httpx.MockTransport(api.handler))
    with open(path, newline="", encoding="utf-8") as fh:
        again = {r["question_id"]: r for r in csv.DictReader(fh)}
    assert again["q1"]["answer_grade"] == "correct" and again["q1"]["notes"] == "checked p5"
    assert again["q3"]["answer_grade"] == ""
    answers = (tmp_path / "run" / "eval_answers.jsonl").read_text().splitlines()
    assert len(answers) == 4


def test_question_file_validation(tmp_path):
    with pytest.raises(ev.EvalError, match="author"):
        ev.load_questions(write_questions(tmp_path / "a.csv", author=""))
    p = tmp_path / "b.csv"
    p.write_text(",".join(HEADER) + "\nEXAMPLE-1,PLACEHOLDER q,answerable,yes,,,,Ajay\n",
                 encoding="utf-8")
    with pytest.raises(ev.EvalError, match="placeholder"):
        ev.load_questions(p)


def test_checked_in_template_has_only_placeholder_rows():
    from paperrelay.config import REPO_ROOT

    path = REPO_ROOT / "eval" / "questions_template.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == HEADER and len(rows) == 2
    assert all(r["question_id"].startswith("EXAMPLE") and "PLACEHOLDER" in r["question"]
               for r in rows)
    with pytest.raises(ev.EvalError):
        ev.load_questions(path)
