# Progress

## Session 1 -- 2026-11-10

### Environment (verified)
- Python 3.12.6
- Git 2.56.0.windows.3
- Claude Code (Sonnet 4.6)
- Branch at session start: main (unborn)
- Identity: Ajay-Ankam / ajay3ankam@gmail.com
- Remote at session start: none configured

### Paper verification (verified in session 1)
All three PDFs passed SHA-256 and byte-size checks against data/papers/manifest.json:
- 1706.03762v7: hash OK, size OK (2,215,244 bytes)
- 2410.00037v1: hash OK, size OK (4,694,312 bytes)
- 2507.07955v2: hash OK, size OK (1,375,282 bytes)

### Bootstrap (verified in session 1)
- scripts/bootstrap.ps1 ran without errors.
- git config core.hooksPath = .githooks confirmed.
- .env copied from .env.example; contents not read.
- private/conversations/ created.

---

## Milestone 0 -- feat/bootstrap

**Commit:** 084ff62 feat: add package health endpoint contracts and CI
**Branch:** feat/bootstrap
**Merged to origin/main:** yes, via PR #1 (merge commit c1730fa, 2026-11-10)
**Push state:** pushed and in sync with origin/feat/bootstrap

### Files created
- src/paperrelay/__init__.py
- src/paperrelay/contracts.py -- shared provider/pipeline/event types
- src/paperrelay/api/__init__.py
- src/paperrelay/api/main.py -- FastAPI app, /health endpoint
- src/paperrelay/ui/__init__.py
- src/paperrelay/ui/app.py -- Streamlit API health view (httpx call to /health)
- tests/__init__.py
- tests/test_health.py -- 3 cases
- tests/test_contracts.py -- 9 cases
- pyproject.toml -- pinned deps, ruff/mypy/pytest config
- requirements.txt -- pinned runtime deps
- requirements-dev.txt -- pinned dev/test deps
- .github/workflows/ci.yml -- lint + type check + test, no live secrets

### Tests run (session 1, verified)
12/12 passed, ruff clean, mypy strict clean.

### Additional commit on feat/bootstrap (by Ajay)
defd7eb docs: record bootstrap validation results and remote push status
- Updated docs/progress.md with test results and push state.
- Introduced UTF-8 BOM and encoding artifacts (-- displayed as garbled bytes) due to
  Windows text editor saving with BOM. See Known Issues below.

---

## Milestone 1 -- feat/retrieval

**Commit:** 622dee8 feat: add BM25 retrieval module with provenance and tests
**Branch:** feat/retrieval (branched from feat/bootstrap after defd7eb)
**Push state:** pushed and in sync with origin/feat/retrieval
**Merged to main:** no -- awaiting Ajay's review

### Files created
- src/paperrelay/retrieval/__init__.py
- src/paperrelay/retrieval/extract.py -- PDF page extraction, Chunk dataclass, _split_page
- src/paperrelay/retrieval/settings.py -- three named configs (small/medium/large)
- src/paperrelay/retrieval/index.py -- BM25Index (build, query, save/load via pickle)
- src/paperrelay/retrieval/retrieve.py -- load_index(), retrieve() high-level interface
- tests/conftest.py -- skip_without_pdfs marker, synthetic_index session fixture
- tests/test_retrieval_unit.py -- 24 unit tests (no PDFs needed)
- tests/test_retrieval_integration.py -- 7 integration tests (skip in CI, run locally)
- scripts/build_index.py -- offline index builder for all three settings
- scripts/inspect_extraction.py -- prints representative page samples

### Dependencies added (pinned)
- pypdf==4.3.1
- rank-bm25==0.2.2

### Retrieval settings (verified)
Chunk sizes are in CHARACTERS, not words or model tokens.
- small:  chunk_size=256 chars, overlap=32,  top_k=5
- medium: chunk_size=512 chars, overlap=64,  top_k=5
- large:  chunk_size=1024 chars, overlap=128, top_k=3

### Chunk counts (measured 2026-11-10 on actual PDFs)
- small:  1,832 chunks total across all three papers
- medium: 931 chunks
- large:  485 chunks

### Provenance (verified)
Provenance fields: arxiv_version, page (1-based), start_char, end_char within
pypdf-extracted page text. Round-trip verified: recovering page_text[start_char:end_char]
reproduces stored chunk text exactly (tested chunk 1706.03762v7:p0001:c000005).
Limitation: offsets are within pypdf output, not PDF byte stream. Consistency relies
on stable pypdf extraction across runs; not tested across pypdf versions.

### Extraction defects (observed on actual PDFs)
1. Column interleaving (1706.03762v7): two-column ACL layout causes author names and
   emails to merge -- "avaswani@google.comNoam Shazeer?" -- without separator.
2. Table cell merging (all papers): P8 of 1706.03762v7 shows "ModelBLEU Training Cost"
   and "2.3?10191.4?1020" -- column cells run together, exponent symbols garbled.
3. Equation garbling: Unicode math symbols (e.g. U+2217 asterisk operator) survive in
   the index but print as "?" on cp1252 consoles; they are present in raw chunk text.
4. Diacritic handling (2410.00037v1): "Alexandre D? efossez?" -- accented chars partly
   retained but sometimes broken by space insertion.
No empty pages found across all three papers (0 of 15/66/39 pages empty).

### Tests run (session 1, with actual PDFs, verified)
43/43 passed, ruff clean, mypy strict clean (11 source files).
Integration tests ran locally (53 s); they are skipped in CI (PDFs gitignored).

**Important limitation:** 43 passing tests are smoke tests and schema checks.
They do NOT demonstrate RAG answer quality, citation accuracy, or refusal correctness.
Retrieval smoke test (test_retrieval_attention_term_returns_attention_paper) confirms
BM25 returns 1706.03762v7 as top result for "multi-head attention softmax scaled dot
product" -- a sanity check, not an evaluated quality measure.

---

## Git state (verified 2026-11-10 audit)

Branch / commit graph (after fetch):
  5275223  chore: add handoff setup files and paper manifest         [main, origin/main base]
  084ff62  feat: add package health endpoint contracts and CI
  c1730fa  Merge pull request #1 from Ajay-Ankam/feat/bootstrap      [origin/main HEAD]
  defd7eb  docs: record bootstrap validation results and remote push  [feat/bootstrap HEAD]
  622dee8  feat: add BM25 retrieval module with provenance and tests  [feat/retrieval HEAD]

feat/retrieval contains feat/bootstrap (defd7eb is an ancestor of 622dee8): yes.
origin/main contains milestone 0 (via merge c1730fa): yes.
local main behind origin/main: yes -- local main at 5275223, origin/main at c1730fa.
feat/retrieval not yet merged to main.

Audit documentation (this file and docs/conversation-capture.md) was committed on
feat/retrieval after the audit.

---

## Known issues

1. **Encoding artifacts in Git history:** Commit defd7eb introduced UTF-8 BOM and
   Windows-1252 misinterpretation of em-dashes and other Unicode into docs/progress.md.
   This file is now rewritten with plain ASCII punctuation to avoid recurrence.

2. **Local main behind origin/main:** local main is at 5275223; origin/main is at
   c1730fa (includes feat/bootstrap merge). Run `git switch main && git merge origin/main`
   after current branch work is complete.

3. **Table/equation extraction degrades retrieval for numeric questions:** Chunk text for
   table rows like "24.6 39.92 2.3e19 1.4e20" is garbled; numeric queries will likely
   fail or return wrong passages. This is a known limitation of pypdf text extraction;
   it must be disclosed in Section 8 of the report and in the retrieval trap section.

4. **No offline index on disk yet:** data/indexes/ does not exist until
   `python scripts/build_index.py` is run. The pipeline (Milestone 2) cannot query
   retrieval until the index is built.

---

## Quota used
Zero -- no Groq calls made in any session to date.

---

## Next task
Milestone 2 -- feat/pipeline: provider adapter, 4+ pipeline stages (interpret,
retrieve, draft, verify), cited answer and refusal, streaming events.
Requires Ajay to agree a measured-run quota budget before any real Groq calls.
