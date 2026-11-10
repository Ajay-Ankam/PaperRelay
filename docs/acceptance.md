# Acceptance checklist
This public engineering checklist is sufficient for code reproduction without the private hiring brief.

## Application
- Python FastAPI backend plus Streamlit UI, one shared backend execution path.
- >=4 stages, actual retrieval tool call, multiple Groq calls, bounded branch/loop.
- Exact arXiv versions: 1706.03762v7, 2410.00037v1, 2507.07955v2; hashes/URLs preserved.
- Every factual answer claim has supporting paper/page/passage citations. Unsupported questions refused; missing retrieval evidence is not proof of absence across the library.
- Stand-in can stream, slow, fail, quota-refuse, slow under load. Calibrated from real measurements; unsupported behavior explicitly synthetic.
- Real pilot records latency distribution, provider limits, real 429 and recovery where observed; undocumented unknowns marked not measured.
- Five required scenario rows: steady, burst, sustained overload, per-minute limit halved, daily budget exhausted. Real/stand-in identified. Raw data retained.
- Same >=15 Ajay-authored/reviewed questions across >=3 chunk/top-k/rerank settings. Include unanswerable, table/numeric, cross-paper and ambiguous terminology questions. Preserve reference evidence and manual grades.
- Retry makes-worse example measured through controlled comparison, not invented.
- Every required results table generated; numerical consistency checked.

## Write-up: exact order
1 What I built and why
2 The real service and my stand-in
3 Results
4 Where the time goes
5 When a limit is hit
Retrieval: quality versus cost
How this library will mislead a naive retriever
6 Retries
7 What the person sees
8 What I'm unsure about, what I left out, what I learned
Your feedback on Vaani
How I worked with the assistant

Include one design-defining decision; measured limits/calibration weaknesses; p50/p95/p99 with counts; a surprising measured result; slow request waiting/working/retry breakdown before/after first token; minute/day policies and fairness/degradation; three retrieval traps with example questions; actual 3pm budget exhaustion wording; least certain assumption/consequence; two deliberate omissions; one learning; real Vaani website visit and Ajay's honest feedback.
Final reflection: three actual prompts/why; actual error caught/how; actual overruled decision; distinctly Ajay's contribution. No manufactured incidents. Preserve real history (or exact prompts if export unavailable), including failures and subagent work where available.

## Required tables
1 Limits: RPM, TPM, RPD, TPD, concurrent requests, limit response, recovery. Columns value/evidence/confidence.
2a per scenario: source, sent count, arrival duration seconds, offered/min, first token p50/p95/p99 ms, total p50/p95/p99 ms.
2b same rows: success%, rate-limited%, timeout/given-up%, retries/request, peak queue, tokens/answer, cost/1000 answers. Include errors in supplemental evidence and explain denominator.
3 retrieval setting (piece size/count/rerank), questions, correct+cited%, wrong%, correctly refused%, tokens/answer, first token p50/p95, total p50/p95.
Use milliseconds, one decimal for percentages, not measured where needed. All figures recomputable from results.csv extensions, with event logs as audit evidence. See metrics.md.

## Submission
Maximum two pages of writing including required tables/headings unless organizer clarifies otherwise. Brief also requests half-page reflection: document space conflict, do not silently exceed or make unreadable. Full history is a separate attachment. Code/raw data/history are separate allowed extras. Render and visually inspect final report.
One Drive folder: report.pdf, source.zip (no secrets/virtualenv), results.csv, raw logs/configs/calibration/questions/grades, conversation history, README commands. Anyone-with-link view; form https://forms.gle/Ucxs6ev7ummzk1yK7 with application email. Ajay controls account, sharing and submission. Verify confirmation email. No claim of submission without evidence.
