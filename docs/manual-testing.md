# Manual testing checkpoint (Milestone 2)

All commands are PowerShell, run from the repository root (`C:\Users\ajay ankam\PaperRelay`).
No activation is needed: always call `.\.venv\Scripts\python.exe` directly.
Environment variables set with `$env:NAME = "value"` apply only to that terminal window.

## How the provider is selected

| `PAPERRELAY_PROVIDER` | What answers | `source` label | Evidence folder |
|---|---|---|---|
| unset or `offline` (default) | Offline extractive test double: copies one sentence chosen by keyword overlap; "verifies" by substring match. No network. | `offline-double` | `evidence\offline-double\<run_id>\` (git-ignored) |
| `groq` | Hosted Groq `openai/gpt-oss-20b` | `real` | `evidence\real\<run_id>\` |

`groq` refuses to start unless `PAPERRELAY_REAL_CALL_CEILING` (a positive integer) and
`PAPERRELAY_RUN_ID` are both set, and `GROQ_API_KEY` is present in the environment or `.env`.
The offline double is labelled on every result, in `/config`, in the Streamlit banner and in
every CSV/JSONL row. It is not the Milestone 3 stand-in, and its output is not model quality,
latency or calibration evidence.

Other optional settings: `PAPERRELAY_RETRIEVAL_SETTING` (`small`/`medium`/`large`, default
`medium`; chunk sizes are characters), `PAPERRELAY_DEADLINE_S` (default 60),
`PAPERRELAY_CALL_TIMEOUT_S` (default 30), `PAPERRELAY_GROQ_JSON_MODE` (`stream_prompt`
default; `strict_schema` = non-streamed structured outputs, so no provider first-content timing),
`PAPERRELAY_REASONING_EFFORT` (default `low`).

## Prerequisites

```powershell
.\.venv\Scripts\python.exe --version            # expect Python 3.12.x
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

## 1. Build and load the indexes

```powershell
.\.venv\Scripts\python.exe scripts\build_index.py
```
Expected: three `OK ... sha256=...` lines (the script stops if any PDF fails the manifest hash),
then small 1,832 / medium 931 / large 485 chunks and `Wrote index_meta.json. Done.`
Evidence: `data\indexes\index_meta.json` (hashes, pypdf version, chunk unit = characters).

Load and search without touching the PDFs:
```powershell
.\.venv\Scripts\python.exe -c "from paperrelay.config import Settings; from paperrelay.retrieval.retrieve import Retriever; s = Settings(); r = Retriever.from_dir(s.index_dir, s.manifest_path); print(r.available()); [print(p.chunk_id, round(p.score, 2)) for p in r.search('scaled dot-product attention', 'medium')]"
```
Expected: `['large', 'medium', 'small']` and five `1706.03762v7:p....` chunk ids first.

## 2. Start FastAPI (terminal 1, offline mode)

```powershell
$env:PAPERRELAY_PROVIDER = "offline"
.\.venv\Scripts\python.exe -m uvicorn paperrelay.api.main:app --port 8000
```
Check from another terminal:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/config
```
Expected: `source` = `offline-double`, `model` = `offline-extractive-v1`, an auto-generated `run_id`.

## 3. Start Streamlit (terminal 2)

```powershell
.\.venv\Scripts\python.exe -m streamlit run src\paperrelay\ui\app.py
```
Expected: browser opens. In offline mode a yellow banner says OFFLINE TEST DOUBLE. Ask a question.
Progress lines (interpreting, searching, drafting, checking) appear first. Answer text appears
only after "Verified; delivering checked answer", with numbered citation expanders that show
paper, page and passage. Unsupported questions show a refusal saying the search may have
missed the passage. Operational failures show a red error that makes no claim about the papers.
Things to try: an empty question (button disabled), stopping FastAPI and reloading (connection error).

## 4. Offline smoke tests

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest -q
```
Expected: ruff and mypy clean; 110 tests pass in about 50 s (the PDF integration tests take most
of that time). One test starts a real uvicorn server on a random local port and checks that a
client which gives up mid-request makes the server cancel its in-flight call.

Client runner against the offline server from step 2:
```powershell
.\.venv\Scripts\python.exe -m paperrelay.client.smoke --question "How many attention heads does the base Transformer model use?" --question "What is the boiling point of water at sea level?"
```
Expected: a `NOTE: offline test double` line, one answered row, one `insufficient_evidence` refusal.
Evidence: `evidence\offline-double\<run_id>\` holds `results.csv`, `client_observations.jsonl`,
`calls.jsonl`, `events.jsonl`, `requests.jsonl` and `run_meta.json`. Token columns are blank
because the double reports no usage.

## 5. Real Groq smoke test (explicit opt-in; NOT run by the assistant)

This spends real quota. Run it only after you agree the budget below.
1. Put your key in `.env` with your editor (`GROQ_API_KEY=...`). Never type it in a terminal.
2. Terminal 1 (stop any offline server first):
```powershell
$env:PAPERRELAY_PROVIDER = "groq"
$env:PAPERRELAY_RUN_ID = "real-smoke-01"
$env:PAPERRELAY_REAL_CALL_CEILING = "8"
.\.venv\Scripts\python.exe -m uvicorn paperrelay.api.main:app --port 8000
```
3. Terminal 2. Choose your own two questions, one the papers answer and one they do not. Examples only:
```powershell
Invoke-RestMethod http://127.0.0.1:8000/config
.\.venv\Scripts\python.exe -m paperrelay.client.smoke --max-requests 2 --sleep-between-s 65 --question "How many parallel attention heads does the base Transformer use?" --question "What learning rate did the original BERT paper use for pre-training?"
Get-Content evidence\real\call_ledger.json
Select-String -Path evidence\real\real-smoke-01\*, evidence\real\call_ledger.json -Pattern "gsk_","Bearer","authorization"
```
Expected: `/config` shows `source: real` and `real_calls_used` at most 8. In the ledger, the
`real-smoke-01` entry's `used` value never exceeds its `ceiling` of 8. The `Select-String`
redaction check prints nothing.

About the ledger (`evidence\real\call_ledger.json`): one file records every real run's budget
and usage. Restarting the server with the same run id continues that run's count. Its stored
ceiling can be lowered from the environment but never raised, so a new budget needs a new run id.
While the server runs, it holds an OS lock on `call_ledger.lock`, so a second real-provider
server refuses to start. Calls are counted when reserved, before any network I/O, so a call
that fails before reaching Groq still counts (the count is conservative). An unreadable ledger
stops the server instead of being reset.
4. Stop the server (Ctrl+C) and switch back to offline before other testing:
`$env:PAPERRELAY_PROVIDER = "offline"`.

Evidence to review before committing anything from `evidence\real\real-smoke-01\`:
`results.csv` (client-observed `first_token_ms` and `total_ms`), `calls.jsonl` (per call: stage,
HTTP status, provider first-content ms, usage or unknown, allow-listed rate-limit headers),
`events.jsonl`, `requests.jsonl` (includes internal drafts and verifier verdicts), `run_meta.json`.
Grading the answers is your job; `answer_grade` is left blank.

### Call-count calculation for the proposed ceiling of 8

The implemented call graph has no retries in Milestone 2:
- interpret: 1 call, always.
- draft: 1 call when retrieval returns passages.
- draft_expanded: 1 call, only when the first retrieval is empty or the draft reports
  insufficient evidence (the one bounded expansion).
- verify: 1 call, only when a draft answers and its citations pass deterministic validation.

So one request makes at most 4 calls (`MAX_CALLS_PER_REQUEST = 4`):

| Question | Typical path | Calls |
|---|---|---|
| Supported | interpret, draft, verify | 3 (4 if expansion is needed) |
| Unsupported | interpret, draft (insufficient), draft_expanded (insufficient) | 3 (4 if the model wrongly drafts an answer that then reaches verification; 1 if both searches return nothing) |

Worst case for two questions: 4 + 4 = 8, so a ceiling of 8 fits. The server only admits a request
when at least 4 calls remain, so the ceiling can never cut a request off mid-pipeline. With 8 it
admits exactly two requests. A third is rejected locally with status `rate_limited`, reason
`local_call_ceiling_insufficient_for_request`, and makes no network call.

Known risks in the real smoke test (not yet observed):
- The Groq rate-limits page lists 30 RPM, 1K RPD, 8K TPM and 200K TPD for gpt-oss-20b, but under
  "Developer plan" wording. Whether those are the free-tier values for your account is unverified.
  If 8K TPM applies, two back-to-back questions could exceed it, because each request allows up to
  3,600 completion tokens (interpret 600 + draft 1,200 + draft_expanded 1,200 + verify 600) plus
  prompts. That is why the command waits 65 s between questions. A 429 is recorded and not retried.
- The docs do not say whether reasoning tokens count against `max_completion_tokens`. If they do,
  output cut at the limit (`finish_reason: length`) is reported as a `truncated_model_output`
  operational error, not a refusal.
- Prompt-requested JSON (default `stream_prompt` mode) is not guaranteed. A malformed reply is an
  operational error. `strict_schema` mode guarantees the schema but cannot measure provider first-content.

### Later proposals (budgets for Ajay to decide; nothing here is authorized)

- Latency pilot (after the smoke test passes): about 10 of your questions, sent one at a time
  about 60-65 s apart, with ceiling 40 (10 x 4). It measures the distribution of client
  first_token_ms and total_ms, plus per-stage provider first-content timing and usage, for
  stand-in calibration. Ten samples cannot support a reliable p99.
- Controlled limit probe (separate run id and ceiling, needs a small probe script in Milestone 3):
  minimal-token requests to read the `x-ratelimit-*` headers, then a short burst slightly above
  the documented RPM to see whether a 429 and `retry-after` appear and how long recovery takes.
  A burst of about 35 calls costs about 35 of the documented 1K requests per day. A 429 is not
  guaranteed: account limits may differ from the docs.
- No daily-exhaustion test against the real account. Daily exhaustion is simulated with the stand-in.
