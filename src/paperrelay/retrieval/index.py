"""BM25 index over paper chunks."""

from __future__ import annotations

import pickle
import re
from pathlib import Path
from typing import cast

from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

from paperrelay.retrieval.extract import Chunk


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        corpus = [_tokenize(c.text) for c in chunks]
        self._bm25: BM25Okapi = BM25Okapi(corpus)

    def query(self, text: str, top_k: int) -> list[tuple[float, Chunk]]:
        """Return (score, chunk) pairs, descending by BM25 score, non-zero only."""
        if not text.strip():
            return []
        tokens = _tokenize(text)
        raw_scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            ((float(raw_scores[i]), self._chunks[i]) for i in range(len(self._chunks))),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [(s, c) for s, c in ranked[:top_k] if s > 0.0]

    def chunk_count(self) -> int:
        return len(self._chunks)

    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)

    @classmethod
    def load(cls, path: Path) -> BM25Index:
        with open(path, "rb") as fh:
            return cast(BM25Index, pickle.load(fh))  # noqa: S301
