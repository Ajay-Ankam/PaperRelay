# Next-session handoff (verified 2026-11-12; checkpoint pushed to origin/feat/experiments)

## Goal and dates
PaperRelay is a research assistant over three pinned arXiv papers (1706.03762v7,
2410.00037v1, 2507.07955v2):
- Stack: FastAPI backend, Streamlit UI, BM25 retrieval, Groq `openai/gpt-oss-20b`.
- Personal target: November 12, 2026 IST.
- Official deadline: **November 13, 2026, 12:27 PM IST**.

Requirements: [acceptance.md](acceptance.md) and [metrics.md](metrics.md). The private
brief is `assignment_description.md`, which is git-ignored.

## Git state
- Branch `feat/experiments`. After this checkpoint it is pushed and equal to
  `origin/feat/experiments`. The handoff commit that adds this file has no SHA here;
  check it with `git log -3`.
- The checkpoint pushed earlier work: `348dfd4` (experiment runners), `c39238d`
  (capacity, retries, stand-in), and the merges `d403ab1` and `1187b39`. The checkpoint
  commits add the scenario config fixes, the guard tests and these docs.
- Ajay merged PR #4 (feat/experiments through `bf743ea`, which includes the Linux mypy fix
  and the offline quota-gate fix) into `origin/main` as `7c04d7b`. CI passed on Linux
  before the merge.
- Left untracked on purpose: `evidence/stand-in/standin-steady-20261112-01/`, the aborted
  run. See Evidence.
- Agent worktrees under `.claude/worktrees/` are clean. Their work is integrated.
  - The reliability agent stopped before writing docs/capacity.md and docs/standin.md.
  - The experiment agent stalled (no progress for 600 s) before writing
    docs/experiments.md and docs/evaluation.md. All its committed work (`348dfd4`) was
    integrated; nothing uncommitted was lost.

## Current implementation and known limitations
- Done and tested (offline/stand-in only):
  - pipeline, citations, verification and refusals;
  - admission queue, concurrency, minute/day gates with reservation reconciliation, and
    a shared retry budget;
  - retrieval fault injection;
  - stand-in HTTP service;
  - open-loop load runner and orchestrator;
  - table/slow-trace analysis;
  - resumable evaluation runner with grading sheet;
  - limit probe;
  - submission assembly script.
- `min_attempt_s` guards are now covered:
  - Admission: a request that waited in the queue and has less than `min_attempt_s`
    left ends `queue_deadline` without calls.
  - Minute gate: a wait must leave `min_attempt_s` before the deadline.
  - Each regression test was checked to fail when its guard is disabled.
- Limitations:
  - Coordination is single-process.
  - Day holds check request counts only; day tokens are checked per call.
  - Stand-in calibration v1 is preliminary (6 real calls); its spread, tail, slowdown
    and error rates are assumptions.
  - Stand-in content is synthetic-extractive and is not model quality.
  - Scenario limits are scaled synthetic values (assistant proposal).
  - Stand-in arrival order under concurrency is not reproducible.

## Missing (required)
- Completed runs of the five scenarios: steady, burst, sustained_overload,
  limit_halved, daily_exhausted.
- The retry on/off comparison and the tool-fault run.
- Generated tables (reports/), the slow-request trace, and an independent review of the
  new code.
- Ajay's 15 or more questions and the real evaluation run (needs an approved budget).
- Grading, including the smoke answer.
- Limit-probe evidence; without it, Table 1 must say "not measured".
- Docs: capacity.md, standin.md, experiments.md, evaluation.md.
- The report, Vaani feedback, Ajay's reflection, clean reproduction, and the Drive
  submission.
- Conversation capture is incomplete: the prompt-04 response was never produced (that
  session was interrupted) and is not recreated.

## Interfaces and commands (PowerShell, repo root)
The contract is [interfaces-m3.md](interfaces-m3.md). Capacity and stand-in settings
are listed in it.

```powershell
.\.venv\Scripts\ruff.exe check src tests; .\.venv\Scripts\mypy.exe; .\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build_index.py          # indexes (git-ignored) if data\indexes is missing
.\.venv\Scripts\python.exe -m uvicorn paperrelay.api.main:app --port 8000   # offline by default
.\.venv\Scripts\python.exe -m streamlit run src\paperrelay\ui\app.py
.\.venv\Scripts\python.exe -m paperrelay.experiments.run --scenario experiments\scenarios\steady.json --run-id standin-steady-20261112-02 --api-port 8310 --standin-port 8311
.\.venv\Scripts\python.exe -m paperrelay.experiments.analyze --runs <run dirs> --out reports\
.\.venv\Scripts\python.exe -m paperrelay.experiments.evaluate --help      # real evaluation; NOT authorized yet
.\.venv\Scripts\python.exe -m paperrelay.experiments.limit_probe --help   # NOT authorized
.\.venv\Scripts\python.exe scripts\assemble_submission.py --out submission\
```

Port 8000 was occupied at the last attempt, so use `--api-port` and `--standin-port`.
Run ids must be new: the orchestrator refuses to overwrite an existing run directory.

## Evidence and quota
**Real:** `evidence/real/real-smoke-20261112-01`, run by Ajay.
- 2 requests: one answered, one `insufficient_evidence` refusal.
- 6 calls reserved in the ledger; all 6 were sent and answered HTTP 200.
- 5,311 provider-reported tokens (4,990 prompt + 321 completion); usage known for every call.
- Headers advertise RPD 1000 and TPM 8000; 994 requests remained after the run.
- Account-wide usage outside the ledger is unknown.
- The answer and its citation are **ungraded**; Ajay must review them.

**Offline double:** `evidence/offline-double/*`. Functional checks only, never evidence.

**Stand-in:** only `evidence/stand-in/standin-steady-20261112-01/`, which is ABORTED and
incomplete:
- interrupted about 54 s into a 100 s window;
- 4/16 requests answered;
- no results.csv;
- run on commit `1187b39-dirty`.

It is not committed. Never delete it, never call it successful, and never use it in tables.

**Authorization:**
- Zero additional real Groq calls are authorized.
- The proposed budget in [real-work-plan.md](real-work-plan.md) (≤245 calls, ≤~156K
  tokens) awaits Ajay's approval.
- Never delete or reset `evidence/real/call_ledger.json`, never raise existing run
  ceilings, and never switch models.

## Decisions
**Made by Ajay:**
- Ran the real smoke test (ceiling 8).
- Merged PR #3.
- Authorized no further real calls at this checkpoint.
- Stopped and resumed the specialist agents, and interrupted the first stand-in run.
- Asked for this handoff.

**Proposed by the assistant, not confirmed by Ajay:**
- Gate defaults (24 RPM, 6K TPM, 900 RPD, 150K TPD) and retry defaults (max 2, 0.5 s
  base, 8 s cap).
- Scaled synthetic scenario limits.
- The real budget and pacing plan.
- No separate latency pilot; evaluation calls are reused for calibration.

## Ordered remaining tasks (critical path in bold)
1. Done in this checkpoint: preserved changes committed and pushed.
2. Before every push, run `.\.venv\Scripts\python.exe -m mypy --platform linux` as well as plain `mypy`. CI runs on Linux, so Windows-only constants
   (e.g. `subprocess.CREATE_NEW_PROCESS_GROUP`) must sit inside an
   `if sys.platform == "win32":` block.
3. **Run the stand-in scenarios** with new run ids:
   - required: steady, burst, sustained_overload, limit_halved, daily_exhausted;
   - also retry_on, retry_off and tool_faults.
   Check that every scheduled request has one `results.csv` row and that
   `daily_exhausted` actually hits exhaustion.
4. **Generate the tables** with `analyze`, then inspect reconciliation, the slow-request
   breakdown and the retry comparison. Report the retry outcome as observed.
5. Run an independent quality review of the capacity, stand-in and analysis code, and fix
   material defects. Test the `min_attempt_s` guards.
6. Write docs/capacity.md, standin.md, experiments.md and evaluation.md (short).
7. **Ajay:**
   - Author 15 or more questions in `eval/questions.csv` from `eval/questions_template.csv`.
     Include unanswerable, table/numeric, cross-paper and ambiguous-terminology questions,
     each with reference paper, page and passage.
   - Approve the real budget.
8. **Real evaluation**: 15 or more questions × 3 settings, reused as the latency
   calibration. Then the optional limit probe, once its budget is approved.
9. **Ajay:**
   - Grade the evaluation answers, plus the smoke answer.
   - Visit vaani.dev and write the feedback.
   - Write the reflection and supply the real prompts.
10. Recalibrate the stand-in (v2) from the evaluation calls if time permits. Otherwise the
   final tables cite v1-preliminary.
11. **Write the 2-page report** with the required headings and tables, render the PDF,
    run `assemble_submission`, reproduce from a clean clone, and tag.
12. **Ajay:** create the Drive folder, set anyone-with-link access, submit the form, and
    verify the confirmation email.

**Omit if time is short:**
- a dense or hybrid retriever, re-ranking, UI polish;
- stand-in calibration v2;
- the limit probe. Table 1 then says "not measured" for the observed limit and recovery
  columns; that is an honest disclosure, not a completed experiment.

## Exact next action
Check `git status` and `git log -3`. Then run the first complete stand-in scenario:
```powershell
.\.venv\Scripts\python.exe -m paperrelay.experiments.run --scenario experiments\scenarios\steady.json --run-id standin-steady-20261112-02 --api-port 8310 --standin-port 8311
```
Check that results.csv has 16 rows, one per scheduled request. Then run the other
scenarios with new run ids.

Additional real Groq calls still require an explicitly agreed call and token budget
(proposal: docs/real-work-plan.md). None is authorized.
