# Progress

## Session 1 -- 2026-11-10

### Environment (verified)
- Python 3.12.6
- Git 2.56.0.windows.3
- Claude Code (Sonnet 4.6)
- Branch at session start: main (unborn)
- Identity: Ajay-Ankam / ajay3ankam@gmail.com
- Remote at session start: none configured

### Paper verification (verified in session 1)
All three PDFs passed SHA-256 and byte-size checks against data/papers/manifest.json:
- 1706.03762v7: hash OK, size OK (2,215,244 bytes)
- 2410.00037v1: hash OK, size OK (4,694,312 bytes)
- 2507.07955v2: hash OK, size OK (1,375,282 bytes)

### Bootstrap (verified in session 1)
- scripts/bootstrap.ps1 ran without errors.
- git config core.hooksPath = .githooks confirmed.
- .env copied from .env.example; contents not read.
- private/conversations/ created.

---

## Milestone 0 -- feat/bootstrap

**Commit:** 084ff62 feat: add package health endpoint contracts and CI
**Branch:** feat/bootstrap
**Merged to origin/main:** yes, via PR #1 (merge commit c1730fa, 2026-11-10)
**Push state:** pushed and in sync with origin/feat/bootstrap

### Files created
- src/paperrelay/__init__.py
- src/paperrelay/contracts.py -- shared provider/pipeline/event types
- src/paperrelay/api/__init__.py
- src/paperrelay/api/main.py -- FastAPI app, /health endpoint
- src/paperrelay/ui/__init__.py
- src/paperrelay/ui/app.py -- Streamlit API health view (httpx call to /health)
- tests/__init__.py
- tests/test_health.py -- 3 cases
- tests/test_contracts.py -- 9 cases
- pyproject.toml -- pinned deps, ruff/mypy/pytest config
- requirements.txt -- pinned runtime deps
- requirements-dev.txt -- pinned dev/test deps
- .github/workflows/ci.yml -- lint + type check + test, no live secrets

### Tests run (session 1, verified)
12/12 passed, ruff clean, mypy strict clean.

### Additional commit on feat/bootstrap (by Ajay)
defd7eb docs: record bootstrap validation results and remote push status
- Updated docs/progress.md with test results and push state.
- Introduced UTF-8 BOM and encoding artifacts (-- displayed as garbled bytes) due to
  Windows text editor saving with BOM. See Known Issues below.

---

## Milestone 1 -- feat/retrieval

**Commit:** 622dee8 feat: add BM25 retrieval module with provenance and tests
**Branch:** feat/retrieval (branched from feat/bootstrap after defd7eb)
**Push state:** pushed and in sync with origin/feat/retrieval
**Merged to main:** no -- awaiting Ajay's review

### Files created
- src/paperrelay/retrieval/__init__.py
- src/paperrelay/retrieval/extract.py -- PDF page extraction, Chunk dataclass, _split_page
- src/paperrelay/retrieval/settings.py -- three named configs (small/medium/large)
- src/paperrelay/retrieval/index.py -- BM25Index (build, query, save/load via pickle)
- src/paperrelay/retrieval/retrieve.py -- load_index(), retrieve() high-level interface
- tests/conftest.py -- skip_without_pdfs marker, synthetic_index session fixture
- tests/test_retrieval_unit.py -- 24 unit tests (no PDFs needed)
- tests/test_retrieval_integration.py -- 7 integration tests (skip in CI, run locally)
- scripts/build_index.py -- offline index builder for all three settings
- scripts/inspect_extraction.py -- prints representative page samples

### Dependencies added (pinned)
- pypdf==4.3.1
- rank-bm25==0.2.2

### Retrieval settings (verified)
Chunk sizes are in CHARACTERS, not words or model tokens.
- small:  chunk_size=256 chars, overlap=32,  top_k=5
- medium: chunk_size=512 chars, overlap=64,  top_k=5
- large:  chunk_size=1024 chars, overlap=128, top_k=3

### Chunk counts (measured 2026-11-10 on actual PDFs)
- small:  1,832 chunks total across all three papers
- medium: 931 chunks
- large:  485 chunks

### Provenance (verified)
Provenance fields: arxiv_version, page (1-based), start_char, end_char within
pypdf-extracted page text. Round-trip verified: recovering page_text[start_char:end_char]
reproduces stored chunk text exactly (tested chunk 1706.03762v7:p0001:c000005).
Limitation: offsets are within pypdf output, not PDF byte stream. Consistency relies
on stable pypdf extraction across runs; not tested across pypdf versions.

### Extraction defects (observed on actual PDFs)
1. Column interleaving (1706.03762v7): two-column ACL layout causes author names and
   emails to merge -- "avaswani@google.comNoam Shazeer?" -- without separator.
2. Table cell merging (all papers): P8 of 1706.03762v7 shows "ModelBLEU Training Cost"
   and "2.3?10191.4?1020" -- column cells run together, exponent symbols garbled.
3. Equation garbling: Unicode math symbols (e.g. U+2217 asterisk operator) survive in
   the index but print as "?" on cp1252 consoles; they are present in raw chunk text.
4. Diacritic handling (2410.00037v1): "Alexandre D? efossez?" -- accented chars partly
   retained but sometimes broken by space insertion.
No empty pages found across all three papers (0 of 15/66/39 pages empty).

### Tests run (session 1, with actual PDFs, verified)
43/43 passed, ruff clean, mypy strict clean (11 source files).
Integration tests ran locally (53 s); they are skipped in CI (PDFs gitignored).

**Important limitation:** 43 passing tests are smoke tests and schema checks.
They do NOT demonstrate RAG answer quality, citation accuracy, or refusal correctness.
Retrieval smoke test (test_retrieval_attention_term_returns_attention_paper) confirms
BM25 returns 1706.03762v7 as top result for "multi-head attention softmax scaled dot
product" -- a sanity check, not an evaluated quality measure.

---

## Git state at the time of the audit (history; see Milestone 2 for current state)

Branch / commit graph (after fetch):
  5275223  chore: add handoff setup files and paper manifest         [main, origin/main base]
  084ff62  feat: add package health endpoint contracts and CI
  c1730fa  Merge pull request #1 from Ajay-Ankam/feat/bootstrap      [origin/main HEAD]
  defd7eb  docs: record bootstrap validation results and remote push  [feat/bootstrap HEAD]
  622dee8  feat: add BM25 retrieval module with provenance and tests  [feat/retrieval HEAD]

feat/retrieval contains feat/bootstrap (defd7eb is an ancestor of 622dee8): yes.
origin/main contains milestone 0 (via merge c1730fa): yes.
local main behind origin/main: yes -- local main at 5275223, origin/main at c1730fa.
feat/retrieval not yet merged to main.

Audit documentation (this file and docs/conversation-capture.md) was committed on
feat/retrieval after the audit.

---

## Milestone 2 -- feat/pipeline (implemented 2026-11-10; awaiting human testing)

### Git integration (verified)
- Audit docs committed on feat/retrieval as 2bbfba0 and pushed.
- Local main fast-forwarded to origin/main (c1730fa, PR #1). Bootstrap was not merged again.
- feat/retrieval (which includes defd7eb) merged into main with --no-ff as d1f0d40, after
  ruff, mypy and 43/43 tests passed on the merge result. main pushed.
- feat/pipeline created from d1f0d40. It is not merged into main; that waits on human testing.

### Implemented
- Pipeline (src/paperrelay/pipeline/runner.py): interpret (model) -> BM25 retrieval tool ->
  draft (model) -> at most one evidence expansion (changed query, doubled top_k, second
  draft) -> deterministic citation validation -> verify (model) -> post-verification delivery.
  At most 4 provider calls per request. No retries (NoRetry policy is the Milestone 3 hook).
- One request deadline (default 60 s) covers all stages. Each call's timeout is capped by the
  remaining deadline. Client disconnect cancels the pipeline task and in-flight HTTP call,
  and both are logged as cancelled.
- Every branch ends in one terminal event: done, refused, error (including rate_limited),
  timeout; cancelled is logged only. Insufficient evidence gives a refusal that does not
  claim absence. Verification transport or format failure gives an operational error.
- Draft text, search queries and verifier verdicts stay in server logs. Answer text is
  streamed only after verification passes, labelled delivery=post_verification.
- Providers: GroqProvider (raw httpx, one shared AsyncClient, include_reasoning=false,
  reasoning deltas ignored, max_completion_tokens, SSE parsing with malformed-stream
  detection, allow-listed rate-limit headers and retry-after). OfflineExtractiveProvider is a
  deterministic double labelled offline-double. CeilingGuardProvider plus CallLedger is a
  persistent hard ceiling, reserved before any network I/O.
- Real calls require PAPERRELAY_PROVIDER=groq, PAPERRELAY_REAL_CALL_CEILING and
  PAPERRELAY_RUN_ID.
- Measurement: server writes calls.jsonl, events.jsonl, requests.jsonl and run_meta.json
  (git revision, config hash, prompt version, index meta) under evidence/<source>/<run_id>/.
  The client runner (python -m paperrelay.client.smoke) is the only writer of results.csv
  (required columns in order, plus metrics.md extensions). It measures first_token_ms
  (first answer_token at the client) and total_ms. Unknown usage is left blank, never 0.
- Indexes: build_index.py now verifies manifest SHA-256 and size, extracts each PDF once,
  and writes index_meta.json. The server checks those hashes against the manifest at
  startup and never extracts PDFs during a request. Pickles are local generated artifacts only.
- Streamlit: an HTTP client of /config and /ask. It shows progress, the checked answer,
  citation expanders, and refusal/error states, plus an offline-double warning banner.
- Groq parameters checked against console.groq.com docs on 2026-11-10. Key finding: json_schema
  structured outputs cannot stream, so the default mode streams JSON requested in the prompt.

### Independent review findings and resolutions
A quality-reviewer subagent inspected the first implementation (read-only). Resolutions:
1. Verifier verdicts where `supported` and `reason` disagree: a claim now passes only when
   `supported` is true AND `reason` is "supported". Tested.
2. Client timeout was recorded as a generic error: it is now status `timeout`, reason
   `client_abandoned`. Other transport errors carry their exception name, and unparseable
   stream lines are recorded and skipped. Tested.
3. results.csv was written once at the end of a run: it is now appended per request, so
   an interrupted run keeps its completed rows.
4. Disconnect cancellation was only tested by closing the generator directly. The API now wraps
   the stream in `contextlib.aclosing`, and a test through a real uvicorn socket shows that a
   client which gives up after 0.5 s makes the server cancel the in-flight call
   (`requests.jsonl`: cancelled / client_disconnected; `calls.jsonl`: ok, cancelled).
5. / 6. The call ledger lived per run directory, and its writes were not atomic. It is now one
   file (`evidence/real/call_ledger.json`) with per-run entries. Writes are atomic (temp file +
   os.replace). A run's ceiling can be lowered but never raised. An OS lock stops a second
   process from using it. Requests are admitted only if 4 calls remain after other admitted
   requests' holds. An unreadable ledger is an error, never a reset. Tested.
7. A provider timeout fired by the deadline clamp was reported as a call timeout: it is now
   reported as `request_deadline`. Tested.
8. Timing assertion widened (>= 250 ms, plus an explicit ordering check) for coarse Windows timers.
9. httpx timeouts are now ProviderTimeout. Connect/pool failures are recorded as `sent=false`.
   Tested.
10. provider_first_token_ms in results.csv is documented as the first sent call (interpret)
    measured from that call's send; a real 0.0 is no longer blanked. Tested.
11. Token columns were blank when no terminal event reached the client: they now fall back to
    the server's request record, so tokens already spent still count. Tested.
12. Ceiling refusals now use status `rate_limited` (metrics.md admission rule) with reasons
    `local_call_ceiling_insufficient_for_request` / `local_call_ceiling_reached`. Tested.
13. `finish_reason` is recorded per call. Output cut at the token limit is reported as
    `truncated_model_output:<stage>`, including the case where reasoning used the whole
    budget and no content arrived. Tested.
14. The server now refuses indexes whose recorded chunk size/overlap differ from the code's
    settings, or whose settings were not recorded. Tested.
15. Streamlit rendered answer text as Markdown: it now uses plain text. A non-200 response
    shows the body and skips stream parsing. Unreadable lines are counted.
16. BM25 search runs in a worker thread so it does not block the event loop. The interpret
    output limit was raised to 600 tokens.
Also found while fixing: `PipelineRunner.stream` cancelled its pending queue getter without
awaiting it ("Event loop is closed" warning). All cancellations are now awaited, and the suite
runs with unraisable-exception warnings treated as errors.

### Checks run (verified)
- ruff check src tests: clean. mypy (strict, 24 files): clean.
- pytest full suite including PDF integration tests, with
  `-W error::pytest.PytestUnraisableExceptionWarning`: 110 passed, three consecutive runs
  (about 50 s each). Remaining warnings are third-party deprecation notices
  (starlette testclient, websockets).
- End-to-end offline run after the fixes: uvicorn (port 8765) with the offline double plus the
  smoke client over real HTTP. One request answered after verification, one
  insufficient_evidence refusal, results.csv written under evidence/offline-double/ (git-ignored).
- In an earlier offline run, the double's "answer" was a verbatim sentence about varying the
  number of heads, which does not answer the question. This shows that substring
  "verification" checks support, not correctness.
- `ruff check scripts` reports a pre-existing import-order issue in scripts/check_commit.py
  (unchanged on this branch). CI lints only src and tests.

### Limitations / open risks
- No real Groq call has been made. Real behaviour (JSON compliance, latency, usage reporting
  in streams, 429 format) is unobserved.
- Groq limit figures (30 RPM, 1K RPD, 8K TPM, 200K TPD for gpt-oss-20b) come from the docs
  page, under Developer-plan wording. The free-tier values are unconfirmed.
- Model verification is fallible. It is not proof of factual accuracy.
- No retries, admission queue, minute/day reservations or stand-in yet (Milestone 3).
- Single API worker: request admission holds are in memory. The ledger's OS lock makes a
  second real-provider process refuse to start rather than share the budget.
- The client reads server JSONL from the local evidence folder to fill extension columns.
  If the client runs on another machine, those columns stay blank.
- Table and equation text extraction defects (above) are unmitigated. Their effect on
  numeric questions is not yet measured.

---

## Real smoke test -- real-smoke-20261112-01 (run by Ajay, 2026-11-12)

Human-reported results, verified by the assistant against `evidence/real/real-smoke-20261112-01/`
(results.csv, requests.jsonl, calls.jsonl):

| Question | Outcome | first_token_ms | total_ms | tokens in/out | calls |
|---|---|---|---|---|---|
| How many parallel attention heads does the base Transformer use? | answered: "The base Transformer uses eight parallel attention heads. [1]" | 5484.0 | 5484.0 | 1699 / 177 | interpret, draft, verify |
| What learning rate did the original BERT paper use for pre-training? | refused: insufficient_evidence (after one expansion) | not applicable | 2218.0 | 3291 / 144 | interpret, draft, draft_expanded |

- Per-call provider first content (ms, from send): interpret 2671.0 and 531.0; draft 2078.0
  and 468.0; draft_expanded 1000.0; verify 500.0. Every call's first content arrived within
  about 16-47 ms of its end: with prompt-requested JSON and hidden reasoning, content
  appears only after reasoning finishes.
- Header-advertised values (from the redacted allow-listed headers): limit-requests 1000
  (RPD per Groq docs), limit-tokens 8000 (TPM per Groq docs). This is header evidence,
  not an observed limit hit; no 429 occurred.
- Secret scan of the evidence directory (patterns gsk_, Bearer, authorization,
  GROQ_API_KEY, api_key, set-cookie): 0 matching files.
- Clock resolution: these timings were taken with `time.monotonic()`, which ticks about
  every 15.6 ms on Windows (values are multiples of ~15.6 ms). Later code uses
  `time.perf_counter()`; the smoke values are kept unchanged.
- Manual grading is pending: the assistant has not graded the answer or checked the
  citation against the PDF. `answer_grade` stays blank until Ajay grades it.

---

## Known issues

1. **Encoding artifacts in Git history:** Commit defd7eb introduced a UTF-8 BOM and
   mojibake in docs/progress.md. The file has been rewritten in ASCII without a BOM.
   PowerShell 5.1 `Set-Content -Encoding utf8` writes a BOM, so do not use it for repo files.

2. **Resolved:** local main now tracks origin/main (see Milestone 2).

3. **Table/equation extraction:** table rows and exponents are garbled in chunk text.
   Numeric or table questions are expected to retrieve poorly. Not measured yet; must be
   disclosed in the report.

4. **Resolved:** indexes are built (data/indexes/, git-ignored) and verified at load.

---

## Quota used
- Real smoke run `real-smoke-20261112-01` (run by Ajay on 2026-11-12): 6 provider calls
  reserved and sent (ledger `used=6`, `ceiling=8`), all HTTP 200. Provider-reported usage:
  4,990 prompt + 321 completion = 5,311 tokens. Headers after the 6th call showed
  `x-ratelimit-remaining-requests: 994` of `1000`. Account-wide usage outside this
  project's ledger is not visible here and may be higher.
- No other real calls in any session.

---

## Next task
Done: real smoke test (6 calls). Next: merge feat/pipeline into main, then Milestone 3/4
on an integration branch (capacity controls, retry budget, stand-in, experiment and
evaluation runners, tables). No further real calls are authorized at this checkpoint.
