"""High-level retrieval interface: load a named index and run a query."""

from __future__ import annotations

from pathlib import Path

from paperrelay.retrieval.extract import Chunk
from paperrelay.retrieval.index import BM25Index
from paperrelay.retrieval.settings import get_config

_DEFAULT_INDEX_DIR = Path("data/indexes")


def index_path(setting_name: str, index_dir: Path = _DEFAULT_INDEX_DIR) -> Path:
    cfg = get_config(setting_name)
    return index_dir / f"bm25_{cfg.name}.pkl"


def load_index(setting_name: str = "default", index_dir: Path = _DEFAULT_INDEX_DIR) -> BM25Index:
    path = index_path(setting_name, index_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"Index not found at {path}. Run:  python scripts/build_index.py"
        )
    return BM25Index.load(path)


def retrieve(
    query: str,
    setting_name: str = "default",
    index_dir: Path = _DEFAULT_INDEX_DIR,
) -> list[tuple[float, Chunk]]:
    cfg = get_config(setting_name)
    idx = load_index(setting_name, index_dir)
    return idx.query(query, cfg.top_k)
