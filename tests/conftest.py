"""Shared fixtures and markers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperrelay.retrieval.extract import Chunk
from paperrelay.retrieval.index import BM25Index

_PDF_DIR = Path("data/papers")
_MANIFEST = Path("data/papers/manifest.json")


def _pdfs_present() -> bool:
    if not _MANIFEST.exists():
        return False
    papers = json.loads(_MANIFEST.read_text())
    return all((_PDF_DIR / f"{p['arxiv_version']}.pdf").exists() for p in papers)


skip_without_pdfs = pytest.mark.skipif(
    not _pdfs_present(),
    reason="PDF files not present (gitignored); run locally with actual PDFs",
)


@pytest.fixture(scope="session")
def synthetic_index() -> BM25Index:
    """Three-chunk in-memory index for unit tests; no PDFs required."""
    chunks = [
        Chunk(
            chunk_id="paper1v1:p0001:c000000",
            arxiv_version="paper1v1",
            page=1,
            start_char=0,
            end_char=50,
            text="attention mechanism transforms query key value softmax",
        ),
        Chunk(
            chunk_id="paper2v1:p0001:c000001",
            arxiv_version="paper2v1",
            page=1,
            start_char=0,
            end_char=50,
            text="speech recognition audio encoder decoder streaming",
        ),
        Chunk(
            chunk_id="paper3v1:p0001:c000002",
            arxiv_version="paper3v1",
            page=1,
            start_char=0,
            end_char=50,
            text="dynamic chunking hierarchical sequence boundary detection",
        ),
    ]
    return BM25Index(chunks)
