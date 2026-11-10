"""Retrieval tool: loads prebuilt local indexes once and answers search calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from paperrelay.retrieval.extract import Chunk
from paperrelay.retrieval.index import BM25Index
from paperrelay.retrieval.settings import CONFIGS, RetrievalConfig, get_config


class IndexLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    arxiv_version: str
    page: int
    start_char: int
    end_char: int
    text: str
    score: float

    @classmethod
    def from_chunk(cls, chunk: Chunk, score: float) -> Passage:
        return cls(
            chunk.chunk_id, chunk.arxiv_version, chunk.page,
            chunk.start_char, chunk.end_char, chunk.text, score,
        )


class Retriever:
    def __init__(self, indexes: dict[str, BM25Index], meta: dict[str, object] | None = None):
        self._indexes = indexes
        self._by_id = {name: {c.chunk_id: c for c in idx.chunks()} for name, idx in indexes.items()}
        self.meta = meta or {}

    @classmethod
    def from_dir(cls, index_dir: Path, manifest_path: Path) -> Retriever:
        """Load indexes written by scripts/build_index.py.

        Pickles are trusted only because this process generated them locally;
        never point index_dir at uploaded or downloaded files.
        """
        meta_path = index_dir / "index_meta.json"
        if not meta_path.exists():
            raise IndexLoadError(f"{meta_path} missing; run scripts/build_index.py")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {p["arxiv_version"]: p["sha256"] for p in manifest}
        if meta.get("papers") != expected:
            raise IndexLoadError("index_meta.json paper hashes do not match manifest; rebuild indexes")
        indexes: dict[str, BM25Index] = {}
        built = meta.get("settings", {})
        for name, cfg in CONFIGS.items():
            path = index_dir / f"bm25_{name}.pkl"
            if not path.exists():
                continue
            # top_k is applied at query time; chunking is baked into the index file.
            b = built.get(name, {})
            if (b.get("chunk_size_chars"), b.get("overlap_chars")) != (cfg.chunk_size, cfg.overlap):
                raise IndexLoadError(
                    f"bm25_{name}.pkl was built with chunk_size={b.get('chunk_size_chars')} "
                    f"overlap={b.get('overlap_chars')}, code expects {cfg.chunk_size}/"
                    f"{cfg.overlap}; rebuild indexes"
                )
            indexes[name] = BM25Index.load(path)
        if not indexes:
            raise IndexLoadError(f"no bm25_*.pkl indexes in {index_dir}")
        return cls(indexes, meta)

    def available(self) -> list[str]:
        return sorted(self._indexes)

    def config(self, setting: str) -> RetrievalConfig:
        cfg = get_config(setting)
        if cfg.name not in self._indexes:
            raise KeyError(f"index for setting {cfg.name!r} not loaded")
        return cfg

    def search(self, query: str, setting: str, top_k: int | None = None) -> list[Passage]:
        cfg = self.config(setting)
        hits = self._indexes[cfg.name].query(query, top_k or cfg.top_k)
        return [Passage.from_chunk(chunk, score) for score, chunk in hits]

    def get_chunk(self, setting: str, chunk_id: str) -> Chunk | None:
        return self._by_id[self.config(setting).name].get(chunk_id)
