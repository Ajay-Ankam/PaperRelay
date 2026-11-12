# Retrieval-quality evaluation (real model outputs)

**Status (2026-11-12):** not run. No real calls are authorized. The budget below is a
proposal for Ajay to approve or change. Grades are only Ajay's.

## 1. Where Ajay supplies the questions
- Copy `eval/questions_template.csv` to **`eval/questions.csv`** (the evaluation input) and
  replace the two `EXAMPLE-*` rows.
- Schema: exact header, in this order. Extra columns are ignored. UTF-8, with or
  without a BOM, so saving from Excel as "CSV UTF-8" works.

| Column | Required | Rule (enforced by `evaluate.load_questions`) |
|---|---|---|
| `question_id` | yes | unique and non-blank; must not start with `EXAMPLE` |
| `question` | yes | the exact question text; must not contain `PLACEHOLDER` |
| `category` | yes | one of `answerable`, `unanswerable`, `table_numeric`, `cross_paper`, `ambiguous_terminology` |
| `expected_answerable` | yes | `yes` or `no` |
| `reference_paper` | for answerable | arXiv id with version: `1706.03762v7`, `2410.00037v1` or `2507.07955v2` (several separated by `;` for cross-paper) |
| `reference_page` | for answerable | PDF page number(s) |
| `reference_passage` | yes | the exact supporting passage copied from the PDF; for unanswerable rows, why no passage supports an answer |
| `author` | yes (non-blank) | who wrote the row; see provenance below |

- Requirements: at least 15 questions, including unanswerable, table/numeric,
  cross-paper and ambiguous-terminology ones.
- **Provenance:** the 15 candidate questions drafted with ChatGPT are AI-assisted
  candidates, not Ajay-authored questions. If Ajay keeps one, the `author` value must
  say so truthfully, for example `Ajay (edited AI-assisted candidate)`. The reference
  page and passage must be checked against the pinned PDF by Ajay. Nobody else
  supplies reference answers.
- **Freeze:** after the dry run, do not edit `eval/questions.csv`. Its SHA-256 is
  recorded in the run metadata, and all three settings use the same frozen file.

## 2. Dry run (no real calls)
This was verified on 2026-11-12 with 2 synthetic questions (author
`synthetic-test (not Ajay)`, evidence kept in scratch, not committed): 6 rows, unique
ids, and a grading sheet was written.
```powershell
# terminal 1: stand-in
.\.venv\Scripts\python.exe -m paperrelay.standin --config standin\config-default.json --port 8321
# terminal 2: API on the stand-in, retries off
$env:PAPERRELAY_PROVIDER="standin"; $env:PAPERRELAY_STANDIN_URL="http://127.0.0.1:8321/openai/v1"
$env:PAPERRELAY_RUN_ID="standin-evaldry-01"; $env:PAPERRELAY_RETRY_MAX="0"; $env:PAPERRELAY_RETRY_MIN_ATTEMPT_S="5"
.\.venv\Scripts\python.exe -m uvicorn paperrelay.api.main:app --port 8320 --workers 1
# terminal 3: evaluation
.\.venv\Scripts\python.exe -m paperrelay.experiments.evaluate --questions eval\questions.csv --settings small,medium,large --run-id standin-evaldry-01 --api http://127.0.0.1:8320 --expected-source stand-in --max-calls 180 --max-tokens 160000 --worst-case-calls 4 --worst-case-tokens-per-request 9000 --sleep-between-s 55 --dry-run
```
- Output goes to `evidence/stand-in/standin-evaldry-01/`, with the grading sheet in
  the run directory.
- Stand-in answers are synthetic. They check plumbing only: never grade them.
- Run with `--sleep-between-s 1` instead for a fast plumbing check. See §4 for what
  that does to the timing.

## 3. Real run plan (proposal; requires Ajay's approval)
45 pipeline executions: the same 15 frozen questions × small, medium and large,
one request each, sequential.

**Retry policy: off** (`PAPERRELAY_RETRY_MAX=0`) for this controlled comparison.
- Every setting then gets exactly one attempt per stage.
- A transient failure is recorded as an operational failure: not gradable, kept in the
  denominator and shown in the table. It is never re-asked silently.
- The runner is resumable, and pairs already in `results.csv` (including failures) are
  skipped, so a failed pair is not retried within the run.

**Per-request bounds**, from code and real usage:
- Calls: interpret 1 + draft 1 + draft_expanded 0-1 + verify 0-1, so at most 4 with
  retries off (6 with the default `RETRY_MAX=2`).
- Completion caps, from `pipeline/runner.py`: interpret 600, draft 1200,
  draft_expanded 1200, verify 600, giving 3,600.
- gpt-oss reasoning tokens are counted inside completion tokens and capped by the same
  limit. Observed completions were 47-67 at `reasoning_effort=low`.
- Real prompt tokens observed (`real-smoke-20261112-01`, medium): interpret 262-265,
  draft 1,038-1,076, draft_expanded 1,950, verify 399.
- Prompt upper bounds:

  | Stage | Bound (tokens) | Basis |
  |---|---|---|
  | interpret | 400 | |
  | draft | 1,400 | large: 3×1,024 chars vs medium 5×512 |
  | draft_expanded | 2,600 | large: 6×1,024 chars |
  | verify | 1,000 | |
  | **Total** | **5,400** | |

- **Worst case: about 9,000 tokens per request** (5,400 prompt + 3,600 completion).
  This is a chosen bound, not a measurement. It is passed as
  `--worst-case-tokens-per-request 9000`.

| | Calls | Tokens |
|---|---|---|
| Expected (smoke: 3 calls and 2,656 tokens per request; n=2, low confidence) | ~135 | ~120K |
| Worst case, retries off | 180 | 405K (cannot complete inside the budget; the runner stops first) |
| Worst case, retries on (6 calls) | 270 | higher |

Proposed real settings:
- `PAPERRELAY_PROVIDER=groq`, `PAPERRELAY_RUN_ID=real-eval-01`,
  `PAPERRELAY_REAL_CALL_CEILING=180`.
- `PAPERRELAY_RETRY_MAX=0` and `PAPERRELAY_RETRY_MIN_ATTEMPT_S=5`.
- `PAPERRELAY_TPD=170000`. The default of 150,000 would refuse requests before the
  runner's 160,000 budget is reached; 170,000 is still under the documented 200K.
- Other gate settings stay at their defaults: 24 RPM, 6,000 TPM, 900 RPD.

- Evaluate with `--max-calls 180 --max-tokens 160000 --worst-case-calls 4
  --worst-case-tokens-per-request 9000 --sleep-between-s 55`.
- The runner stops **before** any request whose worst case would push it past 180 calls
  or 160,000 tokens. With a 9,000 worst case it stops once about 151K tokens are used.
  Unknown usage is charged at the worst case.
- **Existing consumption:** 6 calls and 5,311 tokens in the ledger on 2026-11-12 (UTC).
  Usage from outside this project on the same Groq account is invisible.
- Documented account limits: 1K RPD and 200K TPD. 160K plus the known 5.3K leaves
  about 35K of margin for unseen usage. Whether Groq's day is a UTC calendar day or a
  rolling window is not documented here: treat it as unknown.
- If the runner stops on budget or on a day-scope 429, resume the same command with the
  same run id in a later quota window. Do not raise the ceilings.

**Pacing (derived, not measured).**
- The local minute gate reserves `prompt_chars/3 + completion cap` before each call:
  about 2,400 tokens for a draft and 3,500 for a large expanded draft. It reconciles to
  actual usage afterwards.
- In the synthetic dry run with 1 s spacing, later requests waited up to 49 s in
  `rate_wait`, and one hit the 60 s deadline mid-call.
- A request uses about 2.7-3.5K actual tokens, so the previous request must mostly slide
  out of the 60 s window before the next one starts.
- Hence `--sleep-between-s 55`. That gives about 1 request per minute, about 3K actual
  tokens per minute against the advertised 8K TPM, and about 4 calls per minute
  against the documented 30 RPM.
- **Runtime estimate: 45 × (55 s + 3-15 s request + ≤5 s record wait) ≈ 50-60 minutes.**
  This comes from 2 real smoke requests and the gate arithmetic. It is not a measurement.

## 4. Latency calibration reuse
- Every real evaluation attempt is one record in the run's `calls.jsonl` (stage, attempt,
  HTTP status, provider first-content ms, total ms, usage).
- `python -m paperrelay.standin.calibrate --evidence evidence\real\real-eval-01 --out
  standin\calibration-v2.json` derives per-stage parameters from those records. It
  references the source file by hash, and the calibration's sample unit is the provider
  call.
- The 45 request rows stay as they are in the evaluation `results.csv`. No extra request
  rows are created, and the calibration is not a second experiment.
- What it can and cannot support:
  - The samples are about 135 calls nested in 45 requests over 15 questions, so they are
    correlated: report n per stage and do not treat them as independent.
  - Requests were sequential with one in flight, so the samples describe *unloaded*
    per-stage latency only. Load slowdown, tails under concurrency and limit behaviour
    remain stand-in assumptions.
  - Pacing waits (`rate_wait`) are excluded from provider timing by construction:
    provider times are measured per call.

## 5. Limit probe (separate run; optional)
- Run id `real-probe-01`, ceiling 45 calls, about 6K tokens. Tiny prompt, minimal
  completion cap; reasoning tokens may still count.
- Method: about 1.5 s spacing until the first 429, then at most 5 recovery calls spaced
  by `retry-after`.
- It never targets daily exhaustion. If no 429 occurs, report "no limit observed up to
  N calls".
- Run it only after the evaluation, and only if the day's remaining budget allows.
  Without it, Table 1 says "not measured".
- The command is in `python -m paperrelay.experiments.limit_probe --help`.

## 6. Grading (Ajay only)
- The grading sheet is `eval/grades_real-eval-01.csv`, with grades `correct`, `wrong`,
  `refused_correctly` and `refused_wrongly`. Operational failures are marked not
  gradable and are never pre-graded.
- The smoke answer in `evidence/real/real-smoke-20261112-01` also still needs Ajay's grade.
