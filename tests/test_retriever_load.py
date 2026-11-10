"""Index loading: no PDF extraction at request time; stale indexes rejected."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperrelay.retrieval import extract
from paperrelay.retrieval.index import BM25Index
from paperrelay.retrieval.retrieve import IndexLoadError, Retriever
from tests.fakes import CHUNKS

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "data" / "papers" / "manifest.json"
REAL_INDEX = REPO / "data" / "indexes"


MEDIUM_BUILT = {"medium": {"chunk_size_chars": 512, "overlap_chars": 64, "top_k": 5}}


def _write_index(tmp_path: Path, papers: dict, settings: dict | None = None) -> Path:
    BM25Index(CHUNKS).save(tmp_path / "bm25_medium.pkl")
    meta = {"papers": papers, "settings": MEDIUM_BUILT if settings is None else settings}
    (tmp_path / "index_meta.json").write_text(json.dumps(meta))
    return tmp_path


def _manifest_hashes() -> dict:
    return {p["arxiv_version"]: p["sha256"] for p in json.loads(MANIFEST.read_text())}


def test_loading_and_searching_never_extracts_pdfs(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("PDF extraction attempted at request time")

    monkeypatch.setattr(extract, "PdfReader", boom)
    retriever = Retriever.from_dir(_write_index(tmp_path, _manifest_hashes()), MANIFEST)
    hits = retriever.search("parallel attention heads", "default")
    assert hits and hits[0].arxiv_version == "1706.03762v7"
    assert retriever.get_chunk("medium", hits[0].chunk_id) is not None


def test_index_built_from_other_pdf_versions_is_rejected(tmp_path):
    papers = {**_manifest_hashes(), "1706.03762v7": "0" * 64}
    with pytest.raises(IndexLoadError):
        Retriever.from_dir(_write_index(tmp_path, papers), MANIFEST)


@pytest.mark.parametrize("built", [
    {"medium": {"chunk_size_chars": 1024, "overlap_chars": 64}},  # chunking changed since build
    {},  # index file present but its build settings were never recorded
])
def test_index_built_with_other_chunk_settings_is_rejected(tmp_path, built):
    with pytest.raises(IndexLoadError):
        Retriever.from_dir(_write_index(tmp_path, _manifest_hashes(), built), MANIFEST)


def test_missing_meta_is_rejected(tmp_path):
    BM25Index(CHUNKS).save(tmp_path / "bm25_medium.pkl")
    with pytest.raises(IndexLoadError):
        Retriever.from_dir(tmp_path, MANIFEST)


def test_unloaded_setting_is_rejected(tmp_path):
    retriever = Retriever.from_dir(_write_index(tmp_path, _manifest_hashes()), MANIFEST)
    with pytest.raises(KeyError):
        retriever.search("x", "small")


@pytest.mark.skipif(not (REAL_INDEX / "index_meta.json").exists(),
                    reason="run scripts/build_index.py first")
def test_built_indexes_load_and_provenance_matches_meta():
    retriever = Retriever.from_dir(REAL_INDEX, MANIFEST)
    assert retriever.available() == ["large", "medium", "small"]
    assert retriever.meta["chunk_unit"] == "characters"
    hits = retriever.search("scaled dot-product attention softmax", "medium")
    assert hits[0].arxiv_version == "1706.03762v7"
    for hit in hits:
        assert hit.chunk_id.startswith(f"{hit.arxiv_version}:p{hit.page:04d}:")
