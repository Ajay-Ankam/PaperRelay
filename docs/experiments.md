# Load experiments (stand-in)

## One command per scenario
Each run starts three processes: the stand-in, the API (one uvicorn worker) and the
open-loop load runner. It then stops them all. Every run needs a new run id, because
the orchestrator refuses to overwrite a run directory.
```powershell
.\.venv\Scripts\python.exe -m paperrelay.experiments.run --scenario experiments\scenarios\<name>.json --run-id standin-<name>-YYYYMMDD-NN --api-port 8310 --standin-port 8311
```
- Scenarios in `experiments\scenarios\`:
  - required: `steady`, `burst`, `sustained_overload`, `limit_halved`, `daily_exhausted`;
  - comparisons: `retry_on`/`retry_off` (same schedule and seed) and `tool_faults`.
- Real Groq is impossible here. The orchestrator forces `PAPERRELAY_PROVIDER=standin`
  after merging the scenario env, and it removes `GROQ_API_KEY` from the child environment.
- Bounds:
  - each scenario sets `max_runtime_s`;
  - the orchestrator cancels the runner at `max_runtime_s + 30` s;
  - the servers get a 10 s graceful stop, then terminate, then kill.
- Windows: child return code 3 after a `graceful` stop is normal. uvicorn re-raises the
  captured CTRL_BREAK after "Application shutdown complete", and the C runtime exits
  with code 3.

## Output: evidence/stand-in/<run_id>/
| File | Content |
|---|---|
| `results.csv` | one row per scheduled request (brief columns + added columns, docs/metrics.md) |
| `client_observations.jsonl` | client-side timing per request (first progress, first answer token, terminal) |
| `requests.jsonl`, `events.jsonl`, `calls.jsonl` | server records: request summary, NDJSON events, one record per provider attempt |
| `run_events.jsonl` | runner progress (schedule, sends, run_end) |
| `scenario_meta.json`, `run_meta.json`, `orchestrator.json` | scenario + hash, seed, schedule, calibration hash, commit, effective env, process records |
| `standin_config.json`, `standin_state_end.json`, `daily_budget.json`, `*_server_log.txt` | stand-in config/state, daily accounting, server logs |

Checks after every run:
- the row count equals the scheduled count, and request ids are unique;
- `source` is only `stand-in`;
- per-row tokens equal the summed `calls.jsonl` usage;
- `first_token_ms` is blank for refusals and failures.

## Tables
```powershell
.\.venv\Scripts\python.exe -m paperrelay.experiments.analyze --runs evidence\stand-in\<run1> evidence\stand-in\<run2> --out reports\
```
- The analyzer drops `offline-double` rows and refuses to mix sources.
- Percentiles use one documented method. Small-n tails are labelled as unreliable.

## Completed runs
| Run id | Scenario | Status |
|---|---|---|
| standin-steady-20261112-01 | steady | ABORTED (interrupted ~54 s; no results.csv; commit 1187b39-dirty). Never use. |
| standin-steady-20261112-02 | steady | completed: 16/16 rows, commit 9284c1d (clean). See progress.md. |
