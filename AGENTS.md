# Shared engineering contract
CLAUDE.md is the main session entrypoint; this document is explicitly read by every delegated agent. Filename is AGENTS.md, uppercase.

## Ownership
Main session: architecture, shared schemas, integration, commits, remote pushes, progress reporting.
- retrieval-engineer: ingestion, passage provenance, retrieval settings; src/<package>/retrieval/ and related tests.
- reliability-engineer: capacity, deadlines, provider errors, stand-in; assigned capacity/provider/simulator paths.
- experiment-engineer: load runner, raw schemas, analysis, calibration, tables; experiments/, scripts/analysis, related tests.
- quality-reviewer: read-only independent review; findings cite file/line, impact, reproduction, missing evidence.
Define shared request/result/provider contracts before concurrent work. At most two editing subagents at once. Use separate worktrees/branches for concurrent writers; otherwise serialize. No overlapping shared-file edits. No nested delegation, real API calls, Git mutations, quota spending, or arbitrary dependencies by specialists.

## Return contract
Each agent returns files changed, behavior, tests actually run, observed failures, assumptions, and next integration requirement. No invented success. Human answer grades and reflection remain Ajay's responsibility.

## Git
Use feat/bootstrap, feat/retrieval, feat/pipeline, feat/capacity, feat/simulator, feat/experiments, feat/ui, docs/submission as needed. Keep branches short, dependencies explicit; don't create them all upfront. Merge locally with --no-ff after tests/review; push main and branch. Never merge a branch whose required predecessor is absent. Delete merged branches only when agreed. Commit hook enforces 8-10 words and no Co-authored-by trailers. Do not rewrite published history. Release tag v1.0.0-submission after reconciliation and clean reproduction.

## Public vs private
Track instructions, source, lockfile, tests, configurations, paper hashes, sanitized evidence and reproduction scripts. Ignore private brief, credentials, virtualenv, local caches and unredacted transcripts. Submission assembly must explicitly include sanitized conversation history and all raw evidence; ignored does not mean omitted from submission. Do not blanket-ignore CSV, JSONL, docs, or results.
