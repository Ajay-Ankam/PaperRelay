"""Retrieval unit tests – no PDF files required."""

from __future__ import annotations

import pytest

from paperrelay.retrieval.extract import _split_page
from paperrelay.retrieval.index import BM25Index, _tokenize
from paperrelay.retrieval.settings import CONFIGS, get_config

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def test_tokenize_lowercases():
    assert _tokenize("Hello World") == ["hello", "world"]


def test_tokenize_strips_punctuation():
    tokens = _tokenize("query: key=value!")
    assert "query" in tokens
    assert "key" in tokens
    assert "value" in tokens
    assert ":" not in tokens


def test_tokenize_empty_returns_empty():
    assert _tokenize("") == []


def test_tokenize_numbers_kept():
    assert "1706" in _tokenize("arXiv:1706.03762v7")


# ---------------------------------------------------------------------------
# Chunk splitter
# ---------------------------------------------------------------------------

def test_split_short_text_produces_one_chunk():
    chunks = _split_page("hello world", "v1", 1, 256, 32, 0)
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"


def test_split_produces_multiple_chunks_for_long_text():
    text = "A" * 100
    chunks = _split_page(text, "v1", 1, 30, 5, 0)
    assert len(chunks) > 1


def test_split_chunk_ids_are_unique():
    text = "word " * 200
    chunks = _split_page(text, "arxiv1v1", 3, 50, 10, 0)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_split_provenance_fields():
    chunks = _split_page("some text here for testing provenance", "1706.03762v7", 5, 256, 0, 42)
    assert chunks[0].arxiv_version == "1706.03762v7"
    assert chunks[0].page == 5
    assert chunks[0].chunk_id == "1706.03762v7:p0005:c000042"


def test_split_empty_text_returns_empty():
    assert _split_page("", "v1", 1, 256, 32, 0) == []


def test_split_whitespace_only_returns_empty():
    assert _split_page("   \n\t  ", "v1", 1, 256, 32, 0) == []


def test_split_overlap_smaller_produces_more_chunks_than_no_overlap():
    text = "word " * 100
    with_overlap = _split_page(text, "v1", 1, 50, 25, 0)
    no_overlap = _split_page(text, "v1", 1, 50, 0, 0)
    assert len(with_overlap) >= len(no_overlap)


# ---------------------------------------------------------------------------
# BM25 index
# ---------------------------------------------------------------------------

def test_bm25_query_returns_correct_top_result(synthetic_index: BM25Index):
    results = synthetic_index.query("attention key value", top_k=3)
    assert len(results) > 0
    top_score, top_chunk = results[0]
    assert top_chunk.arxiv_version == "paper1v1"


def test_bm25_query_speech_returns_audio_paper(synthetic_index: BM25Index):
    results = synthetic_index.query("speech audio streaming", top_k=3)
    assert results[0][1].arxiv_version == "paper2v1"


def test_bm25_empty_query_returns_empty(synthetic_index: BM25Index):
    assert synthetic_index.query("", top_k=5) == []


def test_bm25_whitespace_query_returns_empty(synthetic_index: BM25Index):
    assert synthetic_index.query("   ", top_k=5) == []


def test_bm25_nonsense_returns_empty(synthetic_index: BM25Index):
    results = synthetic_index.query("zzzzxxxxxqqqqwwww", top_k=5)
    assert results == []


def test_bm25_scores_descending(synthetic_index: BM25Index):
    results = synthetic_index.query("attention key softmax", top_k=3)
    scores = [s for s, _ in results]
    assert scores == sorted(scores, reverse=True)


def test_bm25_chunk_count(synthetic_index: BM25Index):
    assert synthetic_index.chunk_count() == 3


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_all_three_settings_present():
    assert set(CONFIGS.keys()) == {"small", "medium", "large"}


def test_default_resolves_to_medium():
    cfg = get_config("default")
    assert cfg.name == "medium"


def test_unknown_setting_raises():
    with pytest.raises(KeyError):
        get_config("nonexistent")


def test_chunk_sizes_ordered():
    assert CONFIGS["small"].chunk_size < CONFIGS["medium"].chunk_size < CONFIGS["large"].chunk_size


def test_overlaps_ordered():
    assert CONFIGS["small"].overlap < CONFIGS["medium"].overlap < CONFIGS["large"].overlap


def test_top_k_positive():
    for cfg in CONFIGS.values():
        assert cfg.top_k > 0
