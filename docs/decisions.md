# Decision log
Record date, question, alternatives, chosen policy, evidence, trade-off and decision owner.
Confirmed by Ajay: personal project; Python + FastAPI + Streamlit; complete November 12; feature branches; short 8-10-word commits; no AI commit attribution; retain actual assistant history.
Proposed until tested: verify-before-delivery, single worker, bounded queue/deadline, BM25 baseline plus evaluated stronger retrieval candidate. No latency numbers or quality targets established by evidence yet.

## 2026-11-10 Milestone 2 implementation choices (proposed by the assistant; not yet confirmed by Ajay)
- Partial verification: if any claim is unsupported, refuse the whole answer rather than deliver only the supported claims. Trade-off: more refusals, but no stitched partial answers. Evidence: none yet.
- JSON output mode: default `stream_prompt` (streamed reply, JSON requested in the prompt, validated in code). This keeps provider first-content timing measurable. Groq docs say json_schema structured outputs cannot stream; `strict_schema` stays an opt-in alternative. Trade-off: possible malformed replies (operational errors) vs. no provider streaming timing. Evidence: none yet.
- Retrieval always runs, even if interpretation suggests the question is out of scope. A model's scope judgement is not proof of absence. Cost: one draft call on unsupported questions.
- No retries in Milestone 2 (`NoRetry` policy). The bounded retry budget is Milestone 3 work.
- Real-call admission needs 4 remaining calls (the per-request maximum), so the hard ceiling never truncates a request mid-pipeline.
