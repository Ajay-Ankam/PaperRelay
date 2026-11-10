"""Inspect extracted text quality around difficult regions.

Run from the repository root:
    python scripts/inspect_extraction.py

Prints samples from each paper to show extraction defects:
- Column interleaving (Attention Is All You Need, two-column ACL layout)
- Equation garbling (all three papers)
- Table cell merging (Moshi, Dynamic Chunking)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from paperrelay.retrieval.extract import extract_pages  # noqa: E402
from paperrelay.retrieval.settings import CONFIGS  # noqa: E402

MANIFEST = REPO_ROOT / "data" / "papers" / "manifest.json"
PDF_DIR = REPO_ROOT / "data" / "papers"

SEP = "=" * 70


def _show_page(label: str, text: str, chars: int = 400) -> None:
    snippet = text[:chars].replace("\n", "↵")
    print(f"\n  [{label}]")
    print(f"  {snippet!r}")


def main() -> None:
    papers: list[dict[str, str]] = json.loads(MANIFEST.read_text())

    for paper in papers:
        arxiv_version = paper["arxiv_version"]
        pdf_path = PDF_DIR / f"{arxiv_version}.pdf"
        if not pdf_path.exists():
            print(f"\n{SEP}\n{arxiv_version}: PDF not found — skipping")
            continue

        pages = extract_pages(pdf_path, arxiv_version)
        total_chars = sum(len(t) for _, t in pages)
        empty_pages = sum(1 for _, t in pages if not t.strip())

        print(f"\n{SEP}")
        print(f"Paper : {arxiv_version}")
        print(f"Pages : {len(pages)}  |  total chars: {total_chars:,}  |  empty pages: {empty_pages}")

        _show_page("page 1 (title/abstract)", pages[0][1])
        if len(pages) >= 3:
            _show_page("page 3 (early body)", pages[2][1])
        if len(pages) > 10:
            mid = len(pages) // 2
            _show_page(f"page {mid} (middle)", pages[mid][1])
        _show_page("last page (references/appendix)", pages[-1][1])

        # Count chunks per setting to give a sense of density
        print("\n  Chunk density:")
        for name, c in CONFIGS.items():
            from paperrelay.retrieval.extract import extract_chunks
            n = len(extract_chunks(pdf_path, arxiv_version, c.chunk_size, c.overlap))
            print(f"    {name:6s} (chunk={c.chunk_size}, overlap={c.overlap}): {n} chunks")

    print(f"\n{SEP}")
    print("Inspect the snippets above for:")
    print("  - Interleaved columns  (text jumping mid-sentence to another column)")
    print("  - Garbled equations    (symbols, subscripts as gibberish)")
    print("  - Merged table cells   (numbers running together without whitespace)")
    print("  - Header/footer noise  (page numbers, running titles in body text)")


if __name__ == "__main__":
    main()
