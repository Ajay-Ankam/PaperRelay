# Next session handoff (engineering state)

## Verified state
- Branch: `feat/pipeline` (from main d1f0d40), pushed to origin. Not merged into main;
  merging waits on Ajay's human testing.
- main = d1f0d40 (bootstrap + retrieval + audit docs), pushed.
- Real Groq calls made to date: **0**. Quota spent: none.
- Indexes built locally in `data/indexes/` (git-ignored); rebuild with
  `.\.venv\Scripts\python.exe scripts\build_index.py`.

## Implemented interfaces
- `paperrelay.contracts`: `ProviderCall`, `Usage` (None = unknown), `PipelineRequest`,
  `PipelineResult`, `Citation`, `Claim`, `PipelineEvent`, `EventKind`, `TERMINAL_KINDS`, `Source`.
- `paperrelay.providers.base.ChatProvider` protocol: `complete(ChatRequest, CallContext) -> ChatResult`.
  Every attempt is reported through `CallContext.record`. Errors: `ProviderTimeout`,
  `ProviderRateLimited`, `ProviderMalformed`, `ProviderHTTPError`, `CallCeilingReached`.
- Implementations: `GroqProvider`, `OfflineExtractiveProvider` (offline-double),
  `CeilingGuardProvider` + `CallLedger(path, run_id, ceiling, hold_lock=True)`
  (`admit`/`release` per request, `reserve` per call, `close` at shutdown).
- `paperrelay.pipeline.runner.PipelineRunner.stream(req)` yields `PipelineEvent`s.
  `PipelineDeps.retry_policy` (`RetryPolicy` protocol, default `NoRetry`) is the
  Milestone 3 retry hook. `MAX_CALLS_PER_REQUEST = 4`.
- `paperrelay.retrieval.retrieve.Retriever.from_dir(index_dir, manifest)`, with `.search()`
  and `.get_chunk()`.
- API: `GET /health`, `GET /config`, `POST /ask` (NDJSON stream). App factory:
  `paperrelay.api.main.create_app(Components | None)`.
- Client runner: `python -m paperrelay.client.smoke` (sole writer of results.csv).

## Test commands
```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest -q
```
Last verified result: 110 passed (three consecutive runs, about 50 s each), ruff and mypy
clean. The PDF integration tests need `data/papers/*.pdf` and are skipped without them.
The real-index load test needs `data/indexes/`. One test binds a random local port for a
real uvicorn server.

## Evidence paths
- Offline runs: `evidence/offline-double/<run_id>/` (git-ignored; never evidence).
- Real runs: `evidence/real/<run_id>/` with `results.csv`, `client_observations.jsonl`,
  `calls.jsonl`, `events.jsonl`, `requests.jsonl`, `run_meta.json`. One shared ledger for
  all real runs: `evidence/real/call_ledger.json` (its `.lock`/`.tmp` files are git-ignored).
  Review and redaction-check before committing.

## Open issues
- Real behaviour is unobserved: JSON compliance of gpt-oss-20b in `stream_prompt` mode,
  where usage appears in streams, 429 body/headers, whether reasoning tokens count against
  `max_completion_tokens`.
- Free-tier limits are unconfirmed. The docs list 30 RPM / 1K RPD / 8K TPM / 200K TPD under
  Developer-plan wording.
- Table and equation extraction defects are unmitigated and their effect is unmeasured.
- Single-worker assumption: admission holds are in memory; the ledger lock refuses a second
  real-provider process. Review findings 1-16 and their resolutions are in docs/progress.md.
- Milestone 3 work outstanding: queue/admission, minute/day reservations, retry budget, stand-in.

## Exact next action
Ajay runs docs/manual-testing.md steps 1-4 (offline). Then Ajay decides whether to approve the
real smoke test (step 5, ceiling 8 calls, two questions of Ajay's choosing). Do not start
Milestone 3 or any real calls before that decision.
