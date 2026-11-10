"""Offline script: extract text from PDFs and build BM25 indexes.

Run from the repository root:
    python scripts/build_index.py           # builds all three settings
    python scripts/build_index.py --setting medium
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from paperrelay.retrieval.extract import extract_chunks, extract_pages  # noqa: E402
from paperrelay.retrieval.index import BM25Index  # noqa: E402
from paperrelay.retrieval.settings import CONFIGS  # noqa: E402

MANIFEST = REPO_ROOT / "data" / "papers" / "manifest.json"
PDF_DIR = REPO_ROOT / "data" / "papers"
INDEX_DIR = REPO_ROOT / "data" / "indexes"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline BM25 indexes for PaperRelay")
    parser.add_argument(
        "--setting",
        choices=[*CONFIGS, "all"],
        default="all",
        help="Which setting to build (default: all)",
    )
    args = parser.parse_args()

    papers: list[dict[str, str]] = json.loads(MANIFEST.read_text())
    cfg_names = list(CONFIGS) if args.setting == "all" else [args.setting]

    print("Verifying PDFs …")
    for paper in papers:
        pdf = PDF_DIR / f"{paper['arxiv_version']}.pdf"
        if not pdf.exists():
            sys.exit(f"ERROR: missing {pdf} — run scripts/bootstrap.ps1 or restore PDFs")
        print(f"  OK  {paper['arxiv_version']}  ({pdf.stat().st_size:,} bytes)")

    print("\nExtracting pages (once per paper) …")
    pages_by_paper: dict[str, list[tuple[int, str]]] = {}
    for paper in papers:
        arxiv_version = paper["arxiv_version"]
        pdf_path = PDF_DIR / f"{arxiv_version}.pdf"
        pages = extract_pages(pdf_path, arxiv_version)
        pages_by_paper[arxiv_version] = pages
        total_chars = sum(len(t) for _, t in pages)
        print(f"  {arxiv_version}: {len(pages)} pages, {total_chars:,} chars")

    for cfg_name in cfg_names:
        cfg = CONFIGS[cfg_name]
        print(f"\nBuilding '{cfg.name}' index (chunk={cfg.chunk_size}, overlap={cfg.overlap}) …")
        all_chunks = []
        for paper in papers:
            arxiv_version = paper["arxiv_version"]
            pdf_path = PDF_DIR / f"{arxiv_version}.pdf"
            chunks = extract_chunks(pdf_path, arxiv_version, cfg.chunk_size, cfg.overlap)
            print(f"  {arxiv_version}: {len(chunks)} chunks")
            all_chunks.extend(chunks)
        idx = BM25Index(all_chunks)
        path = INDEX_DIR / f"bm25_{cfg.name}.pkl"
        idx.save(path)
        print(f"  Saved → {path}  ({idx.chunk_count():,} total chunks)")

    print("\nDone.")


if __name__ == "__main__":
    main()
