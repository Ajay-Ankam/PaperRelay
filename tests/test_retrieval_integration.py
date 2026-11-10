"""Retrieval integration tests – require actual PDF files (skipped in CI).

Run locally after verifying PDFs against manifest hashes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperrelay.retrieval.extract import extract_chunks, extract_pages
from paperrelay.retrieval.index import BM25Index
from paperrelay.retrieval.settings import CONFIGS
from tests.conftest import skip_without_pdfs

PDF_DIR = Path("data/papers")
MANIFEST = Path("data/papers/manifest.json")
pytestmark = skip_without_pdfs


@pytest.fixture(scope="module")
def papers() -> list[dict]:  # type: ignore[type-arg]
    return json.loads(MANIFEST.read_text())


def test_all_papers_extract_without_error(papers: list[dict]) -> None:  # type: ignore[type-arg]
    for p in papers:
        pages = extract_pages(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"])
        assert len(pages) > 0, f"{p['arxiv_version']} produced no pages"


def test_each_paper_covers_multiple_pages(papers: list[dict]) -> None:  # type: ignore[type-arg]
    for p in papers:
        pages = extract_pages(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"])
        assert len(pages) >= 5, f"{p['arxiv_version']} has fewer than 5 pages: {len(pages)}"


def test_chunk_counts_differ_across_settings(papers: list[dict]) -> None:  # type: ignore[type-arg]
    counts: dict[str, int] = {}
    for name, cfg in CONFIGS.items():
        total = 0
        for p in papers:
            total += len(
                extract_chunks(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"],
                               cfg.chunk_size, cfg.overlap)
            )
        counts[name] = total
    # smaller chunk size → more chunks
    assert counts["small"] > counts["medium"] > counts["large"], counts


def test_all_arxiv_versions_appear_in_chunks(papers: list[dict]) -> None:  # type: ignore[type-arg]
    cfg = CONFIGS["medium"]
    all_chunks = []
    for p in papers:
        all_chunks.extend(
            extract_chunks(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"],
                           cfg.chunk_size, cfg.overlap)
        )
    found_versions = {c.arxiv_version for c in all_chunks}
    expected = {p["arxiv_version"] for p in papers}
    assert found_versions == expected


def test_chunk_ids_are_globally_unique(papers: list[dict]) -> None:  # type: ignore[type-arg]
    cfg = CONFIGS["medium"]
    all_chunks = []
    for p in papers:
        all_chunks.extend(
            extract_chunks(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"],
                           cfg.chunk_size, cfg.overlap)
        )
    ids = [c.chunk_id for c in all_chunks]
    assert len(ids) == len(set(ids)), "Duplicate chunk IDs found"


def test_retrieval_attention_term_returns_attention_paper(papers: list[dict]) -> None:  # type: ignore[type-arg]
    cfg = CONFIGS["medium"]
    all_chunks = []
    for p in papers:
        all_chunks.extend(
            extract_chunks(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"],
                           cfg.chunk_size, cfg.overlap)
        )
    idx = BM25Index(all_chunks)
    results = idx.query("multi-head attention softmax scaled dot product", top_k=5)
    assert len(results) > 0
    top_version = results[0][1].arxiv_version
    assert top_version == "1706.03762v7", f"Expected attention paper, got {top_version}"


def test_retrieval_provenance_fields_always_present(papers: list[dict]) -> None:  # type: ignore[type-arg]
    cfg = CONFIGS["small"]
    for p in papers:
        chunks = extract_chunks(PDF_DIR / f"{p['arxiv_version']}.pdf", p["arxiv_version"],
                                cfg.chunk_size, cfg.overlap)
        for c in chunks:
            assert c.arxiv_version == p["arxiv_version"]
            assert c.page >= 1
            assert c.chunk_id.startswith(p["arxiv_version"])
            assert len(c.text) > 0
