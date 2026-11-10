# Milestones and stop points
0. Bootstrap: inspect this handoff and installed versions; initialize Python packaging, locked dependencies, lint/test configuration, health endpoint, Streamlit API health view, CI without live secrets. No Groq calls. Branch feat/bootstrap. Commit/push when configured; stop for review.
1. Retrieval: verify three PDFs, offline extraction/provenance, BM25 and three configurable settings. Inspect examples. Branch feat/retrieval.
2. Vertical slice: real provider adapter, interpretation/retrieval/draft/verification, bounded second retrieval, cited answer and refusal; one explicitly budgeted real pilot. Begin collecting telemetry immediately.
3. Reliability: queue/deadline/reservations/cancellation/daily exhaustion; test invariants and restart scope. Stand-in with calibration artifact; no fake calibrated defaults.
4. Experiments: all five scenarios, retry ablation, >=15 questions across >=3 retrieval settings, manual evidence review, raw-to-table generation. Choose retrieval based on evidence. Cold vs warm/cache policies stated.
5. UI: clear question/answer/citation view; progress vs checked text; deadline/cancel/overload/day exhaustion states, no fake dashboards or API key display. Do not add a second model client to UI.
6. Delivery: exact tables, slow-request trace, all required headings, honest personal reflection, Vaani visit, two-page rendered report, clean-environment reproduction, sanitized history and release archive/tag.
Finish Nov 12 IST; Nov 13 12:27 PM IST is the official deadline. Do not claim completion of a milestone until validated. Stop if real quota depleted; stand-in testing continues with clear source labels.
