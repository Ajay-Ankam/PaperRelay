# Capacity control (operational summary)

The authoritative contract is [interfaces-m3.md](interfaces-m3.md) §2-4. This page says
how to run and read the control. It adds no new behaviour.

## What is controlled
| Layer | Unit | Setting (env) | Default | When exceeded |
|---|---|---|---|---|
| Admission queue | pipeline request | `PAPERRELAY_MAX_QUEUE` | 16 | `rate_limited` / `admission_queue_full` |
| Concurrency | pipeline request | `PAPERRELAY_MAX_CONCURRENCY` | 4 | waits in the queue (`waiting_ms`) |
| Request deadline | pipeline request | `PAPERRELAY_DEADLINE_S` | 60 | `timeout` / `queue_deadline` or stage timeout |
| Minute gate | provider call | `PAPERRELAY_RPM` / `_TPM` | 24 / 6000 | waits (`rate_wait`) if the wait fits the deadline, else `rate_limited` |
| Day gate | provider call | `PAPERRELAY_RPD` / `_TPD` | 900 / 150000 | `rate_limited` / `daily_budget_exhausted`, no retry |
| Retries | provider call | `PAPERRELAY_RETRY_MAX` | 2 (0 = off) | one shared budget per request |
| Real-call ledger | real calls only | `PAPERRELAY_REAL_CALL_CEILING` | required for `groq` | refuses further calls |

- Before each call the gate reserves 1 request plus an estimated token count:
  prompt characters / `PAPERRELAY_CHARS_PER_TOKEN` (3.0), plus the stage's full
  `max_completion_tokens` (interpret 600, draft 1200, draft_expanded 1200, verify 600).
  After the call it reconciles the reservation to the provider-reported usage. Unknown
  usage keeps the reservation.
- Admission requires the day budget to cover the worst case per request,
  `4 + RETRY_MAX` calls.
- Daily accounting is persisted to `PAPERRELAY_DAILY_STATE`
  (`daily_budget.json`, UTC day) so a restart does not reset it.

## Limitations (stated, not solved)
- Coordination is in-process. Run one API worker (`--workers 1`). Two processes would
  each believe they own the whole budget. This is not distributed-safe.
- Usage on the same Groq account from outside this process is invisible. The gate
  defaults sit below the documented and header limits (30 RPM, 8K TPM, 1K RPD,
  200K TPD) to leave margin. That margin is a choice, not a measurement.
- Day holds check request counts at admission. Day tokens are checked per call.

## Inspecting it
- `GET /capacity`: the live queue, in-flight requests and gate state.
- `GET /config`: effective limits, retry settings and `max_calls_per_request`.
- Evidence: `events.jsonl` (`queued`, `admitted`, `rate_wait`, `retry_wait`, ...) and
  `calls.jsonl` (one record per attempt, with `attempt`, `retryable` and `limit_scope`).
- Tests: `tests/test_capacity.py` and `tests/test_retry.py` cover admission boundaries,
  reservation/reconciliation, the `min_attempt_s` guards, daily exhaustion and retry deadlines.
