# Stand-in provider service

A separate local HTTP process that speaks the same OpenAI-compatible streaming API as
Groq. The API reaches it through the unchanged `GroqProvider` adapter when
`PAPERRELAY_PROVIDER=standin`. Rows are labelled `source=stand-in`.

## What it is and is not
- **Is:** a controllable source of streaming timing, usage, rate limits (minute and day
  429 with headers), long tails, slowdown under in-flight load, 5xx errors and
  malformed or truncated streams.
- **Is not:** model quality. Content is synthetic-extractive JSON built from the
  supplied passages, so the pipeline mechanics (citations, verification) run end to
  end. **Never cite stand-in answers as answer-quality evidence.**

## Calibration
- `standin/calibration-v1-preliminary.json` covers per-stage first-content and stream
  medians and completion tokens. It comes from **6 real calls** in
  `real-smoke-20261112-01` (`preliminary: true`), so it has no meaningful confidence interval.
- These are synthetic design choices, listed in `standin/config-default.json` → `notes`:
  - lognormal spread;
  - tail probability 0.05 with a ×4 multiplier;
  - +25% slowdown per extra in-flight call;
  - error and malformed rates;
  - chunk count;
  - enforcement semantics.
- A v2 calibration can be built from real evaluation calls (see
  [evaluation.md](evaluation.md#latency-calibration-reuse)):
  `.\.venv\Scripts\python.exe -m paperrelay.standin.calibrate --evidence <real run dir> --out standin\calibration-v2.json`

## Running it
The orchestrator starts and stops it for you (see [experiments.md](experiments.md)).
To run it by hand:
```powershell
.\.venv\Scripts\python.exe -m paperrelay.standin --config standin\config-default.json --port 8311 --log private\standin-manual.log
```
- `--seed` overrides the config seed.
- Scenario configs deep-merge over `config-default.json`. The merged file is saved as
  `standin_config.json` in each run directory.
- Since commit 1daa134, when a scenario sets `calibration_file`, the orchestrator injects
  that file into the stand-in config, and a conflicting calibration fails fast. When a
  scenario overrides `limits`, the inherited "Limits..." note is replaced by one stating
  the effective limits.
- Runs before that commit, including standin-steady-20261112-02, still carry the stale
  default-limits note. Their `limits` object is authoritative.
