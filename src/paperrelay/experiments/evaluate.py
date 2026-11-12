"""Retrieval-quality evaluation runner: Ajay's questions x retrieval settings through /ask.

    python -m paperrelay.experiments.evaluate --questions eval/questions.csv \
        --settings small,medium,large --run-id <id> --api http://127.0.0.1:8000 \
        --expected-source real --max-calls N --max-tokens T --sleep-between-s S

Safety: refuses to start unless /config reports the expected source and the same run id
(the server writes this run's evidence); for source real it also requires a ledger
ceiling. Before every request it stops if the remaining ledger calls (from /config) are
below the worst case per request, or if this run's calls/tokens plus the worst case would
exceed --max-calls/--max-tokens. Unknown usage is charged at the worst case.
Resumable: (question_id, setting) pairs already in the run's results.csv are skipped.

Outputs (run dir = /config evidence_dir): results.csv rows with scenario eval:<setting>,
eval_answers.jsonl (answer, claims, citations, per-attempt calls and usage), and the
grading sheet eval/grades_<run_id>.csv (dry runs: inside the run dir). Grades are left
blank for Ajay; operational failures are marked not gradable and never pre-graded.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from paperrelay.client.smoke import COLUMNS
from paperrelay.config import REPO_ROOT
from paperrelay.experiments.client import await_server_records, build_row, stream_ask

QUESTION_COLUMNS = ["question_id", "question", "category", "expected_answerable",
                    "reference_paper", "reference_page", "reference_passage", "author"]
CATEGORIES = {"answerable", "unanswerable", "table_numeric", "cross_paper",
              "ambiguous_terminology"}
GRADE_COLUMNS = ["request_id", "question_id", "setting", "question", "category",
                 "expected_answerable", "reference_paper", "reference_page",
                 "reference_passage", "answer", "citations", "status", "outcome_reason",
                 "gradable", "answer_grade", "notes"]
DEFAULT_WORST_CALLS = 6  # 4 pipeline calls + default retry budget 2 (docs/interfaces-m3.md)


class EvalError(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int
    max_tokens: int
    worst_calls: int
    worst_tokens: int
    calls_used: int = 0
    tokens_used: int = 0

    def stop_reason(self, ledger_remaining: int | None) -> str | None:
        if ledger_remaining is not None and ledger_remaining < self.worst_calls:
            return (f"ledger_remaining {ledger_remaining} < worst-case calls per request "
                    f"{self.worst_calls}")
        if self.calls_used + self.worst_calls > self.max_calls:
            return (f"calls used {self.calls_used} + worst case {self.worst_calls} would exceed "
                    f"--max-calls {self.max_calls}")
        if self.tokens_used + self.worst_tokens > self.max_tokens:
            return (f"tokens used {self.tokens_used} + worst case {self.worst_tokens} would "
                    f"exceed --max-tokens {self.max_tokens}")
        return None

    def charge(self, provider_calls: int | None, tokens_known: int, usage_complete: bool) -> None:
        self.calls_used += self.worst_calls if provider_calls is None else provider_calls
        self.tokens_used += tokens_known if usage_complete else max(tokens_known,
                                                                    self.worst_tokens)


def load_questions(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in QUESTION_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise EvalError(f"{path} missing columns {missing}")
        rows = list(reader)
    seen = set()
    for r in rows:
        qid = r["question_id"].strip()
        if not qid or qid in seen:
            raise EvalError(f"blank or duplicate question_id {qid!r}")
        seen.add(qid)
        if "PLACEHOLDER" in r["question"].upper() or qid.upper().startswith("EXAMPLE"):
            raise EvalError(f"{qid}: template placeholder row; replace it with a real question")
        if not r["author"].strip():
            raise EvalError(f"{qid}: author is blank (questions must be authored by Ajay)")
        if r["category"].strip() not in CATEGORIES:
            raise EvalError(f"{qid}: category must be one of {sorted(CATEGORIES)}")
        if r["expected_answerable"].strip().lower() not in ("yes", "no"):
            raise EvalError(f"{qid}: expected_answerable must be yes or no")
    return rows


def eval_request_id(run_id: str, question_id: str, setting: str) -> str:
    return hashlib.sha256(f"paperrelay:eval:{run_id}:{question_id}:{setting}".encode()
                          ).hexdigest()[:32]


def read_results(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def append_row(path: Path, row: dict[str, Any]) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _known_tokens(row: dict[str, Any]) -> int:
    total = 0
    for key in ("tokens_in", "tokens_out"):
        try:
            total += int(row.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _usage_complete(row: dict[str, Any]) -> bool:
    return str(row.get("tokens_usage_complete")) == "True"


def _citations_text(citations: list[dict[str, Any]]) -> str:
    return " | ".join(f"{c.get('chunk_id')} (p.{c.get('page')}): {str(c.get('passage', ''))[:300]}"
                      for c in citations)


def write_grade_sheet(path: Path, results: list[dict[str, str]], answers: list[dict[str, Any]],
                      questions: dict[str, dict[str, str]]) -> int:
    """(Re)build the grading sheet, preserving grades/notes Ajay already entered."""
    previous: dict[str, dict[str, str]] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8-sig") as fh:
            previous = {r["request_id"]: r for r in csv.DictReader(fh)}
    by_id = {a["request_id"]: a for a in answers}
    rows = []
    for r in results:
        if not r.get("scenario", "").startswith("eval:"):
            continue
        rid = r["request_id"]
        a = by_id.get(rid, {})
        q = questions.get(r["question_id"], {})
        gradable = r["status"] == "ok"
        prev = previous.get(rid, {})
        rows.append({
            "request_id": rid, "question_id": r["question_id"],
            "setting": r["retrieval_setting"], "question": q.get("question", ""),
            "category": q.get("category", ""),
            "expected_answerable": q.get("expected_answerable", ""),
            "reference_paper": q.get("reference_paper", ""),
            "reference_page": q.get("reference_page", ""),
            "reference_passage": q.get("reference_passage", ""),
            "answer": a.get("answer") or a.get("message") or "",
            "citations": _citations_text(a.get("citations") or []),
            "status": r["status"], "outcome_reason": r.get("outcome_reason", ""),
            "gradable": "yes" if gradable else
            f"no (operational: {r.get('outcome_reason') or r['status']})",
            # Never pre-filled; an operational failure has no assessable answer.
            "answer_grade": prev.get("answer_grade", "") if gradable else "",
            "notes": prev.get("notes", ""),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=GRADE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


async def evaluate(args: argparse.Namespace,
                   transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    questions = load_questions(Path(args.questions))
    if len(questions) < 15:
        print(f"WARNING: {len(questions)} questions; the assignment requires at least 15.")
    settings = [s.strip() for s in args.settings.split(",") if s.strip()]
    async with httpx.AsyncClient(base_url=args.api, transport=transport,
                                 timeout=httpx.Timeout(args.client_timeout_s + 5)) as client:
        cfg = (await client.get("/config")).json()
        source = cfg.get("source")
        if source != args.expected_source:
            raise EvalError(f"/config source {source!r} != --expected-source "
                            f"{args.expected_source!r}")
        if args.dry_run and source == "real":
            raise EvalError("--dry-run is for non-real sources only")
        if cfg.get("run_id") != args.run_id:
            raise EvalError(f"server run_id {cfg.get('run_id')!r} != --run-id {args.run_id!r}; "
                            f"start the API with PAPERRELAY_RUN_ID={args.run_id}")
        if source == "real" and not cfg.get("real_call_ceiling"):
            raise EvalError("source real requires a ledger ceiling (PAPERRELAY_REAL_CALL_CEILING)")
        unknown = [s for s in settings if s not in (cfg.get("retrieval_settings") or settings)]
        if unknown:
            raise EvalError(f"settings {unknown} not available on the server")
        run_dir = Path(cfg["evidence_dir"])
        results_path = run_dir / "results.csv"
        existing = read_results(results_path)
        done = {(r["question_id"], r["retrieval_setting"]) for r in existing
                if r.get("scenario", "").startswith("eval:")}
        worst_calls = args.worst_case_calls or int(cfg.get("max_calls_per_request")
                                                   or DEFAULT_WORST_CALLS)
        budget = Budget(args.max_calls, args.max_tokens, worst_calls,
                        args.worst_case_tokens_per_request)
        # Re-charge what earlier invocations of this run already spent.
        records = await await_server_records(run_dir, {r["request_id"] for r in existing}, 0.0)
        for r in existing:
            rec = records.get(r["request_id"], {})
            budget.charge(_int(rec.get("provider_calls")), _known_tokens(r), _usage_complete(r))
        answers_path = run_dir / "eval_answers.jsonl"
        sent = 0
        stop: str | None = None
        for q in questions:
            for setting in settings:
                if (q["question_id"], setting) in done:
                    continue
                ledger_remaining = None
                if source == "real":
                    live = (await client.get("/config")).json()
                    ceiling, used = live.get("real_call_ceiling"), live.get("real_calls_used")
                    if ceiling is None or used is None:
                        stop = "ledger state unavailable from /config"
                    else:
                        ledger_remaining = int(ceiling) - int(used)
                stop = stop or budget.stop_reason(ledger_remaining)
                if stop:
                    break
                if sent and args.sleep_between_s > 0:
                    await asyncio.sleep(args.sleep_between_s)
                rid = eval_request_id(args.run_id, q["question_id"], setting)
                obs, terminal = await stream_ask(client, request_id=rid, question=q["question"],
                                                 retrieval_setting=setting,
                                                 timeout_s=args.client_timeout_s)
                recs = await await_server_records(run_dir, {rid},
                                                  0.0 if terminal else args.server_record_wait_s)
                server = recs.get(rid)
                row = build_row(obs, terminal, cfg, server, scenario=f"eval:{setting}",
                                question_id=q["question_id"],
                                extra={"run_id": args.run_id, "cost_basis": "not_measured"})
                append_row(results_path, row)
                acct = terminal or server or {}
                calls = [{k: c.get(k) for k in ("stage", "attempt", "status", "http_status",
                                                 "sent", "usage", "retryable", "limit_scope")}
                         for c in (server or {}).get("calls", [])]
                with open(answers_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "request_id": rid, "question_id": q["question_id"], "setting": setting,
                        "status": acct.get("status"), "reason": acct.get("reason"),
                        "message": acct.get("message"), "answer": acct.get("answer"),
                        "claims": acct.get("claims", []), "citations": acct.get("citations", []),
                        "provider_calls": acct.get("provider_calls"), "calls": calls,
                        "client_reason": obs.get("client_reason")}) + "\n")
                budget.charge(_int(acct.get("provider_calls")), _known_tokens(row),
                              _usage_complete(row))
                sent += 1
                print(f"[{q['question_id']} x {setting}] status={row['status']} "
                      f"reason={row['outcome_reason'] or '-'} total_ms={row['total_ms']}")
            if stop:
                break
    answers = []
    if answers_path.exists():
        answers = [json.loads(x) for x in answers_path.read_text(encoding="utf-8").splitlines()
                   if x.strip()]
    grades_path = (run_dir / f"grades_{args.run_id}.csv" if args.dry_run
                   else Path(args.grades_dir) / f"grades_{args.run_id}.csv")
    n = write_grade_sheet(grades_path, read_results(results_path), answers,
                          {q["question_id"]: q for q in questions})
    summary = {"sent": sent, "stopped": stop, "calls_charged": budget.calls_used,
               "tokens_charged": budget.tokens_used, "grade_rows": n,
               "grades_path": str(grades_path), "run_dir": str(run_dir)}
    print(json.dumps(summary))
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PaperRelay retrieval evaluation runner")
    p.add_argument("--questions", required=True)
    p.add_argument("--settings", default="small,medium,large")
    p.add_argument("--run-id", required=True)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--expected-source", required=True,
                   choices=["real", "stand-in", "offline-double"])
    p.add_argument("--max-calls", type=int, required=True)
    p.add_argument("--max-tokens", type=int, required=True)
    p.add_argument("--sleep-between-s", type=float, default=0.0)
    p.add_argument("--worst-case-calls", type=int, default=None,
                   help="override worst-case provider calls per request (default from "
                        "/config max_calls_per_request, else 6)")
    p.add_argument("--worst-case-tokens-per-request", type=int, default=8000,
                   help="conservative token bound per request (a chosen bound, not measured)")
    p.add_argument("--client-timeout-s", type=float, default=120.0)
    p.add_argument("--server-record-wait-s", type=float, default=5.0)
    p.add_argument("--grades-dir", default=str(REPO_ROOT / "eval"))
    p.add_argument("--dry-run", action="store_true",
                   help="plumbing check against a non-real source; grade sheet stays in the "
                        "run dir")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    try:
        asyncio.run(evaluate(args))
    except EvalError as exc:
        raise SystemExit(f"evaluation refused: {exc}") from exc


if __name__ == "__main__":
    main()
