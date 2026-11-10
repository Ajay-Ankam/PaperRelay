"""Named retrieval configurations – chunk size, overlap, top-k."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalConfig:
    name: str
    chunk_size: int  # characters per chunk
    overlap: int     # overlap between consecutive chunks in characters
    top_k: int       # passages returned per query


CONFIGS: dict[str, RetrievalConfig] = {
    "small":  RetrievalConfig("small",   256,  32, 5),
    "medium": RetrievalConfig("medium",  512,  64, 5),
    "large":  RetrievalConfig("large",  1024, 128, 3),
}

DEFAULT_CONFIG = "medium"


def get_config(name: str) -> RetrievalConfig:
    if name == "default":
        return CONFIGS[DEFAULT_CONFIG]
    if name not in CONFIGS:
        raise KeyError(f"Unknown retrieval config {name!r}; choices: {sorted(CONFIGS)}")
    return CONFIGS[name]
