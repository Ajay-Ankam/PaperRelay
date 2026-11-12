# Milestone 3/4 shared interfaces (defined by the main session before delegation)

Status: contract for concurrent work on feat/experiments. Values marked "default" are
explicit design choices, not measured optimums. Change this file only via the main session.

## 1. Sources and labels
- `real` = hosted Groq. `stand-in` = local simulator service (this milestone).
  `offline-double` = in-process development double; never used in tables.
- New provider mode `PAPERRELAY_PROVIDER=standin`: the API uses the existing
  `GroqProvider` (same adapter, same HTTP/SSE parsing) against the stand-in base URL.
  `source="stand-in"`. No Groq key is read in this mode; the adapter sends the fixed
  placeholder key `standin-local-key`. No call ceiling is required for stand-in.

## 2. Server configuration (env vars; reliability-engineer implements in config.py)
| Variable | Default | Meaning |
|---|---|---|
| PAPERRELAY_PROVIDER | offline | offline / groq / standin |
| PAPERRELAY_STANDIN_URL | http://127.0.0.1:8100/openai/v1 | stand-in base URL |
| PAPERRELAY_INDEX_DIR | <repo>/data/indexes | index directory override |
| PAPERRELAY_MAX_CONCURRENCY | 4 | pipeline requests executing at once |
| PAPERRELAY_MAX_QUEUE | 16 | requests waiting for admission; beyond -> rate_limited |
| PAPERRELAY_RPM / _TPM | 24 / 6000 | provider-call minute gate (sliding 60 s) |
| PAPERRELAY_RPD / _TPD | 900 / 150000 | provider-call daily gate (UTC day, persisted) |
| PAPERRELAY_DAILY_STATE | <evidence>/<source>/daily_budget.json | persisted daily accounting |
| PAPERRELAY_RETRY_MAX | 2 | shared retry budget per request (0 = retries off) |
| PAPERRELAY_RETRY_BASE_S / _CAP_S | 0.5 / 8.0 | exponential backoff base / cap (full jitter) |
| PAPERRELAY_RETRY_SEED | 0 | jitter RNG seed (reproducible runs) |
| PAPERRELAY_TOOL_TIMEOUT_S | 5.0 | retrieval tool timeout |
| PAPERRELAY_FAULT_RETRIEVAL | (empty) | e.g. `timeout=0.1,error=0.05,seed=7`; deterministic per request_id |
| PAPERRELAY_ADMIN | 0 | 1 enables POST /admin/limits (local experiments only) |

Gate defaults sit below documented/header values (30 RPM, 8K TPM, 1K RPD, 200K TPD for
gpt-oss-20b) to leave margin for usage this process cannot see.

## 3. Pipeline-request vs provider-call limits
- Pipeline requests: admission queue (`MAX_QUEUE`) + concurrency (`MAX_CONCURRENCY`).
  A request is admitted only if the daily provider budget can cover its worst case
  (`MAX_CALLS_PER_REQUEST = 4 + RETRY_MAX` calls); otherwise it ends `rate_limited`,
  reason `daily_budget_insufficient`. Queue overflow -> `rate_limited`, `admission_queue_full`.
  Deadline expiry while queued -> `timeout`, `queue_deadline`.
- Provider calls: before each attempt, reserve 1 request + estimated tokens
  (prompt estimate + max_completion_tokens) in both minute and day windows; reconcile
  with provider-reported usage after the call; unknown usage keeps the reservation.
  Minute shortfall -> wait (event `rate_wait`) if the wait fits the remaining deadline,
  else `rate_limited`, `minute_budget_wait_exceeds_deadline`. Day shortfall -> no wait,
  `rate_limited`, `daily_budget_exhausted`. Upstream 429 with day scope -> no retry,
  `rate_limited`, `upstream_429_daily:<stage>`; queued requests are then refused too.
- The real-call ledger (`evidence/real/call_ledger.json`) stays as an independent hard
  ceiling for `real` only.

## 4. Retries (one shared budget per request)
Retryable: 429 with minute scope or unknown scope (honour `retry-after`), HTTP 5xx,
transport errors, call timeouts that are not the request deadline, retrieval tool
timeouts. Not retryable: daily exhaustion (local or upstream), 400/401/403/404,
malformed/truncated model output, citation/verification failures, insufficient
evidence, ceiling refusal. Delay = max(retry_after, uniform(0, min(cap, base*2^n)));
skipped if it would not leave time for the attempt before the deadline. Every attempt is
a ProviderCall record (with `attempt`, `retryable`, `limit_scope`) and its known usage
counts.

## 5. Events (NDJSON over POST /ask; contracts.EventKind)
Existing progress kinds plus: `queued`, `admitted`, `rate_wait`, `call_start`, `call_end`,
`retry_wait`, `tool_start`, `tool_end`. Clients ignore kinds they do not render.
Terminal payload = `PipelineResult`, now with `retries`, `queue_wait_ms`, `rate_wait_ms`,
`retry_wait_ms`, `queue_peak_observed`. Server clock: `time.perf_counter()` everywhere.

## 6. HTTP endpoints
- API: `GET /health`, `GET /config` (adds capacity settings), `POST /ask`,
  `GET /capacity` -> `{queue_depth, in_flight, max_concurrency, max_queue,
  limits:{rpm,tpm,rpd,tpd}, minute:{requests,tokens}, day:{date_utc,requests,tokens},
  retry:{max,base_s,cap_s}}`,
  `POST /admin/limits` body `{rpm?,tpm?,rpd?,tpd?}` -> `{applied_utc, limits}` (403 unless
  PAPERRELAY_ADMIN=1).
- Stand-in (separate process, `python -m paperrelay.standin --config <json> --port 8100`):
  `POST /openai/v1/chat/completions` (OpenAI/Groq-compatible SSE and non-stream; same
  error body and `x-ratelimit-*` / `retry-after` headers), `GET /health`,
  `GET /admin/state`, `POST /admin/config` (partial update of limits/faults/latency;
  returns `{applied_utc, config}`).
- Limit halving: the experiment runner calls API `/admin/limits` and stand-in
  `/admin/config` back to back and records both `applied_utc` values.

## 7. Evidence layout
`evidence/<source>/<run_id>/`: run_meta.json, calls.jsonl, events.jsonl, requests.jsonl
(server); results.csv, client_observations.jsonl, scenario_meta.json, run_events.jsonl,
server/stand-in process logs (client/experiment runner). results.csv columns: the 12
required columns, then the metrics.md extension columns, in `paperrelay.client.smoke.COLUMNS`
order. Stand-in calibration: `standin/calibration-*.json` with source run ids and sample counts.

## 8. File ownership on this branch
- reliability-engineer: `src/paperrelay/capacity/**`, `src/paperrelay/standin/**`,
  `src/paperrelay/pipeline/runner.py`, `src/paperrelay/api/main.py`, `src/paperrelay/config.py`,
  `src/paperrelay/providers/**`, `standin/**`, `docs/capacity.md`, `docs/standin.md`,
  tests: `test_capacity*.py`, `test_retry*.py`, `test_standin*.py`, `test_pipeline.py`,
  `test_api_and_client.py`, `test_config.py`, `test_groq_provider.py`, `fakes.py`.
- experiment-engineer: `src/paperrelay/experiments/**`, `experiments/**`, `eval/**`,
  `scripts/assemble_submission.py`, `scripts/analyze.py`, `docs/experiments.md`,
  `docs/evaluation.md`, tests: `test_experiments*.py`, `test_analysis*.py`,
  `test_evaluate*.py`, `tests/fixtures/**`. `src/paperrelay/client/smoke.py` stays
  unchanged (import `COLUMNS`, `_row` helpers from it if useful).
- main session: `contracts.py`, `.gitignore`, `pyproject.toml`, progress/decisions/
  next-session/manual-testing docs, Git.
