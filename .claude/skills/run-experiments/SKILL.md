---
name: run-experiments
description: Run reproducible stand-in load scenarios and retrieval evaluations.
---
Read docs/acceptance.md and docs/metrics.md. Freeze config, seeds, commit and calibration. Use separate backend/simulator/load processes. Run steady, burst, sustained_overload, minute_limit_halved and daily_budget_exhausted with scheduled arrivals. Record lag and abandonments. Compare retry on/off with identical schedule/seed; expose amplification. For retrieval ask same >=15 human-reviewed questions per setting using real outputs. Keep warm/cold and cache policy explicit.
