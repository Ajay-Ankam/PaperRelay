"""PDF text extraction with page/passage provenance.

Known extraction defects (pypdf on these papers):
- 1706.03762v7 (Attention Is All You Need): two-column ACL layout causes pypdf to
  interleave column text on some pages; equations extract as garbled unicode symbols.
- 2410.00037v1 (Moshi): tables and figures lose spatial structure; numeric cells may merge.
- 2507.07955v2 (Dynamic Chunking): headers/footers contaminate body text on some pages.
Run scripts/inspect_extraction.py to view representative samples before indexing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass
class Chunk:
    chunk_id: str        # "{arxiv_version}:p{page:04d}:c{idx:06d}"
    arxiv_version: str
    page: int            # 1-based
    start_char: int      # byte offset within page text
    end_char: int
    text: str


def _split_page(
    text: str,
    arxiv_version: str,
    page: int,
    chunk_size: int,
    overlap: int,
    base_index: int,
) -> list[Chunk]:
    """Split a single page's text into overlapping fixed-size chunks."""
    chunks: list[Chunk] = []
    if not text:
        return chunks
    step = max(1, chunk_size - overlap)
    pos = 0
    local_idx = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        body = text[pos:end].strip()
        if body:
            chunks.append(
                Chunk(
                    chunk_id=f"{arxiv_version}:p{page:04d}:c{base_index + local_idx:06d}",
                    arxiv_version=arxiv_version,
                    page=page,
                    start_char=pos,
                    end_char=end,
                    text=body,
                )
            )
            local_idx += 1
        if end == len(text):
            break
        pos += step
    return chunks


def extract_pages(pdf_path: Path, arxiv_version: str) -> list[tuple[int, str]]:
    """Return [(page_num, text), ...] with 1-based page numbers."""
    reader = PdfReader(str(pdf_path))
    return [(i + 1, reader.pages[i].extract_text() or "") for i in range(len(reader.pages))]


def extract_chunks(
    pdf_path: Path,
    arxiv_version: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """Extract all chunks from one PDF with the given chunking parameters."""
    all_chunks: list[Chunk] = []
    for page_num, page_text in extract_pages(pdf_path, arxiv_version):
        all_chunks.extend(
            _split_page(page_text, arxiv_version, page_num, chunk_size, overlap, len(all_chunks))
        )
    return all_chunks
