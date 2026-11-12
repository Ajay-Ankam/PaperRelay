"""Offline script: verify pinned PDFs, extract text once, build BM25 indexes.

Run from the repository root:
    .\\.venv\\Scripts\\python.exe scripts\\build_index.py           # all three settings
    .\\.venv\\Scripts\\python.exe scripts\\build_index.py --setting medium

Chunk sizes and overlaps are in characters of pypdf-extracted text.
Writes data/indexes/bm25_<setting>.pkl plus data/indexes/index_meta.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pypdf  # noqa: E402

from paperrelay.retrieval.extract import chunk_pages, extract_pages  # noqa: E402
from paperrelay.retrieval.index import BM25Index  # noqa: E402
from paperrelay.retrieval.settings import CONFIGS  # noqa: E402

MANIFEST = REPO_ROOT / "data" / "papers" / "manifest.json"
PDF_DIR = REPO_ROOT / "data" / "papers"
INDEX_DIR = REPO_ROOT / "data" / "indexes"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline BM25 indexes for PaperRelay")
    parser.add_argument("--setting", choices=[*CONFIGS, "all"], default="all")
    args = parser.parse_args()

    papers: list[dict[str, object]] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cfg_names = list(CONFIGS) if args.setting == "all" else [args.setting]

    print("Verifying PDFs against manifest SHA-256 ...")
    for paper in papers:
        pdf = PDF_DIR / f"{paper['arxiv_version']}.pdf"
        if not pdf.exists():
            sys.exit(f"ERROR: missing {pdf}")
        actual = _sha256(pdf)
        if actual != paper["sha256"] or pdf.stat().st_size != paper["bytes"]:
            sys.exit(f"ERROR: {pdf.name} does not match manifest hash/size; refusing to index")
        print(f"  OK  {paper['arxiv_version']}  sha256={actual[:12]}...  {pdf.stat().st_size:,} bytes")

    print("\nExtracting pages (once per paper) ...")
    pages_by_paper: dict[str, list[tuple[int, str]]] = {}
    for paper in papers:
        version = str(paper["arxiv_version"])
        pages = extract_pages(PDF_DIR / f"{version}.pdf", version)
        pages_by_paper[version] = pages
        print(f"  {version}: {len(pages)} pages, {sum(len(t) for _, t in pages):,} chars")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    built: dict[str, int] = {}
    for cfg_name in cfg_names:
        cfg = CONFIGS[cfg_name]
        print(f"\nBuilding '{cfg.name}' (chunk={cfg.chunk_size} chars, overlap={cfg.overlap} chars) ...")
        all_chunks = []
        for version, pages in pages_by_paper.items():
            chunks = chunk_pages(pages, version, cfg.chunk_size, cfg.overlap)
            print(f"  {version}: {len(chunks)} chunks")
            all_chunks.extend(chunks)
        idx = BM25Index(all_chunks)
        path = INDEX_DIR / f"bm25_{cfg.name}.pkl"
        idx.save(path)
        built[cfg.name] = idx.chunk_count()
        print(f"  Saved {path.name} ({idx.chunk_count():,} chunks)")

    meta_path = INDEX_DIR / "index_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update(
        {
            "built_utc": datetime.now(UTC).isoformat(),
            "pypdf_version": pypdf.__version__,
            "chunk_unit": "characters",
            "papers": {str(p["arxiv_version"]): p["sha256"] for p in papers},
        }
    )
    settings_meta = meta.get("settings", {})
    for name, count in built.items():
        cfg = CONFIGS[name]
        settings_meta[name] = {
            "chunk_size_chars": cfg.chunk_size,
            "overlap_chars": cfg.overlap,
            "top_k": cfg.top_k,
            "chunks": count,
        }
    meta["settings"] = settings_meta
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nWrote {meta_path.name}. Done.")


if __name__ == "__main__":
    main()
