---
name: real-service-probe
description: Plan and run a bounded authorized Groq measurement pilot.
---
Read docs/metrics.md. Confirm explicit request/token ceiling and selected model with Ajay before live calls. Record docs/account/header evidence with UTC timestamps, no authorization header. Use streamed requests with usage and provider TTFT. Modestly approach one limit under provider terms; stop after evidence, respect Retry-After, record recovery. Unknown concurrency/TPD stays not measured. Update calibration from actual data only.
