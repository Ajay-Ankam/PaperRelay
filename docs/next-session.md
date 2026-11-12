# Next-session handoff (updated 2026-11-12 after checkpoint 1A-1C; pushed to origin/feat/experiments)

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
- Checkpoint 1A-1C commits, all pushed: `1daa134`, `fe94e7a`, `264d05b`, `8b8b9a5`,
  `3247f25`, plus the handoff commit (see `git log -3`). Not merged into main: the
  experiments are unfinished.
- Left untracked: all of `evidence/stand-in/` (runs -01 to -04). See Evidence for the
  decision Ajay must make.
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
- Completed runs of four scenarios: burst, sustained_overload, limit_halved,
  daily_exhausted. Steady is done (-04).
- The retry on/off comparison and the tool-fault run.
- Generated tables (reports/) and the slow-request trace.
- Ajay's 15 or more questions and the real evaluation run (needs an approved budget).
- Grading, including the smoke answer.
- Limit-probe evidence; without it, Table 1 must say "not measured".
- The report, Vaani feedback, Ajay's reflection, clean reproduction, and the Drive
  submission.
- Conversation capture: the prompt-04 response was never produced (that session was
  interrupted) and is not recreated. Automatic export now runs; see
  [conversation-capture.md](conversation-capture.md). The redacted copies under
  `private/conversations/*/submission_redacted/` need Ajay's review before sharing.

## Interfaces and commands (PowerShell, repo root)
The contract is [interfaces-m3.md](interfaces-m3.md). Capacity and stand-in settings
are listed in it.

```powershell
.\.venv\Scripts\ruff.exe check src tests; .\.venv\Scripts\mypy.exe; .\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build_index.py          # indexes (git-ignored) if data\indexes is missing
.\.venv\Scripts\python.exe -m uvicorn paperrelay.api.main:app --port 8000   # offline by default
.\.venv\Scripts\python.exe -m streamlit run src\paperrelay\ui\app.py
.\.venv\Scripts\python.exe -m paperrelay.experiments.run --scenario experiments\scenarios\steady.json --run-id standin-<name>-YYYYMMDD-NN --api-port 8310 --standin-port 8311
.\.venv\Scripts\python.exe scripts\export_conversations.py --input <transcript.jsonl>   # manual export; the Stop hook does it automatically
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

**Stand-in** (`evidence/stand-in/`, all uncommitted):
- `-01`: ABORTED (interrupted about 54 s in, no results.csv, `1187b39-dirty`). Never
  delete it and never use it in tables.
- `-02`: completed 16/16 on `9284c1d`. Written before the incremental-results fix.
- `-03`: completed 16/16 and passed every data check, but is labelled `8b8b9a5-dirty`
  because docs were edited while it started; the executed code matched `8b8b9a5`.
- **`-04`: the reference steady run.** Clean `3247f25` at start and end; 16/16
  rows/terminal records; 15 answers and 1 refusal; 48 calls; 27,053 tokens reconciled.
- **Decision for Ajay:** the run_meta, scenario_meta, orchestrator and run_events files
  contain absolute paths with the Windows username, and committed evidence has none so
  far. Either (a) commit the raw files as they are, accepting the username, or
  (b) change the runner to record repo-relative paths, re-run steady, and commit only
  clean runs. Raw files are never rewritten.

**Authorization:**
- Zero additional real Groq calls are authorized.
- The proposed budget in [evaluation.md](evaluation.md) §3 (evaluation: 180 calls /
  160K tokens, retries off, 55 s spacing; probe: 45 calls / ~6K tokens) awaits Ajay's
  approval. It supersedes real-work-plan.md for the evaluation.
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
1. Done: preserved changes committed; checkpoint 1A-1C (capture, incremental results,
   steady -04, docs, evaluation plan).
2. Before every push, run `.\.venv\Scripts\python.exe -m mypy --platform linux` as well as plain `mypy`. CI runs on Linux, so Windows-only constants
   (e.g. `subprocess.CREATE_NEW_PROCESS_GROUP`) must sit inside an
   `if sys.platform == "win32":` block.
3. **Run the stand-in scenarios** with new run ids:
   - required: burst, sustained_overload, limit_halved, daily_exhausted (steady done);
   - also retry_on, retry_off and tool_faults.
   Check that every scheduled request has one `results.csv` row and that
   `daily_exhausted` actually hits exhaustion.
4. **Generate the tables** with `analyze`, then inspect reconciliation, the slow-request
   breakdown and the retry comparison. Report the retry outcome as observed.
5. Done: the quality review of the load runner/run integrity and the exporter; material
   defects fixed. Still open: review the capacity and stand-in internals under fault
   scenarios.
6. Done: docs/capacity.md, standin.md, experiments.md and evaluation.md.
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
1. Ajay decides how the stand-in evidence is committed (see Evidence: option a or b).
2. Then run the next scenario. **Do not edit tracked files while a run is going**: any
   tracked change marks the run `-dirty`.
```powershell
.\.venv\Scripts\python.exe -m paperrelay.experiments.run --scenario experiments\scenarios\burst.json --run-id standin-burst-20261112-01 --api-port 8310 --standin-port 8311
```
3. Check results_status.json (`completed`), rows = scheduled, unique ids, clean revision.
4. In parallel, Ajay writes `eval/questions.csv` (schema in [evaluation.md](evaluation.md) §1)
   and runs the stand-in dry run in §2.

Additional real Groq calls still require an explicitly agreed call and token budget
(proposal: docs/evaluation.md section 3). None is authorized.
