# Remaining real work: budget proposal (NOT authorized, NOT run)

> **Superseded for the evaluation run by [evaluation.md](evaluation.md) §3 (2026-11-12).**
> Changes: retries off, a call ceiling of 180, a 160K token budget, a 9,000-token worst
> case and 55 s spacing. The spacing was derived after a stand-in dry run showed that
> 20 s spacing saturates the local 6K TPM gate. The probe section below still stands.

Prepared 2026-11-12 by the assistant. Every real call below needs Ajay's explicit approval
of the stated ceiling first. Zero real calls were made while preparing this.

## Known real usage so far
- Project ledger: `real-smoke-20261112-01` used 6 calls (ceiling 8).
- Provider-reported tokens: 4,990 prompt + 321 completion = 5,311.
- Account-wide usage outside this ledger is invisible here and may be higher.

## Groq limits: documented vs advertised vs observed (checked 2026-11-12)
| Item | Documented (console.groq.com/docs/rate-limits, undated) | Header-advertised (smoke run) | Observed |
|---|---|---|---|
| RPM | 30 | not advertised by headers | not measured |
| RPD | 1K | x-ratelimit-limit-requests 1000 | not measured |
| TPM | 8K | x-ratelimit-limit-tokens 8000 | not measured |
| TPD | 200K | not advertised by headers | not measured |
| Concurrency | not stated | not advertised | not measured |
| Limit response | 429; retry-after in seconds, only on 429 | none seen (no 429) | not measured |

- Plan wording: the docs say the table shows "the base limits for the Developer plan".
  Free-plan values are not shown, so treat the documented numbers as unconfirmed for
  this account. The header values match the documented RPD/TPM.
- Per the docs, request headers refer to RPD and token headers to TPM. Limits apply at
  organization level, and cached tokens do not count toward them.
- 498 (flex capacity) and 503 (overload/maintenance) are documented as "try again later";
  the docs give no retry-after for them.

## Call graph used for the worst case
One request makes these calls:
- interpret: 1
- draft: 1
- draft_expanded: 0-1
- verify: 0-1
- retries: 0-`RETRY_MAX` (default 2) from the shared budget

So the worst case is 6 calls per request (4 with retries off). The admission check
reserves that worst case before a request starts.

Observed per-call prompt tokens (medium setting):
- interpret: 262-265
- draft: 1,038-1,076
- draft_expanded: 1,950
- verify: 399

Completion tokens were 47-67 per call, against limits of 600/1,200/1,200/600.

## 1. Retrieval evaluation (reused as the latency pilot)
Ajay's ≥15 questions × 3 settings (small, medium, large) = 45 requests.

| | Calls | Tokens |
|---|---|---|
| Expected (smoke mean: 3 calls, ~2,700 tokens per request) | ~135 | ~120K |
| Worst case, retries off | 45 × 4 = 180 | 45 × ~8K = ~360K (above the documented 200K TPD) |
| Worst case, retries on | 45 × 6 = 270 | higher still |

Worst-case tokens per request ≈ prompts (~265 + ~1,100 + ~2,400 for the expanded large
setting + ~600) + completion limits (3,600) ≈ 8K.

Proposal:
- Run id `real-eval-01`, ceiling **200 calls**, evaluation token budget **150,000**.
- The runner stops before any request whose worst case would cross either budget.
- Retries on (max 2), because this run is for answer quality.
- If it stops on budget or a day-scope 429, resume the same run on the next quota window;
  the runner is resumable.

Pacing:
- The server gate is set to 6,000 TPM and 24 RPM.
- `--sleep-between-s 20` keeps the average near ~5-6K tokens per minute, below the
  advertised 8K TPM.
- Expected runtime is about 45 × (20 s + ~4 s latency) ≈ 18-25 minutes. That is an
  estimate from two smoke requests, not a measurement.

Latency reuse: every eval call is logged with stage, attempt, provider first-content and
total time. That gives about 135 call-level samples to recalibrate the stand-in
(`calibration-v2`). No separate latency pilot is proposed.

## 2. Controlled limit probe (separate run)
- Run id `real-probe-01`, ceiling **45 calls**, token budget **~6,000** (tiny prompt,
  max_completion_tokens kept minimal; reasoning tokens may still count).
- Method:
  1. Send single calls at ~1.5 s spacing, which crosses the documented 30 RPM within the
     first minute.
  2. Stop at the first 429 and record its headers and retry-after.
  3. Then make at most 5 recovery calls, spaced by retry-after, until one succeeds.
  4. Stop at the ceiling regardless.
- A 429 is not guaranteed; if none occurs, report "no limit observed up to N calls".
- Never targets daily exhaustion.

## Total proposed remaining real budget
- Calls: ≤ 245 (200 eval + 45 probe), on top of the 6 already used.
- Tokens: ≤ ~156K (150K eval + ~6K probe).
- This fits the documented 1K RPD. Tokens could exceed a 200K TPD only together with
  unknown account usage; the runner stops on budget or a day-scope 429 rather than
  forcing through.
