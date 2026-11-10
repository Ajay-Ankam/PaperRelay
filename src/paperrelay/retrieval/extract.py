"""PDF text extraction with page/passage provenance.

Extracted text is untrusted data: it is indexed and quoted, never followed as instructions.

Defects observed on the pinned PDFs (see docs/progress.md):
- Adjacent text runs merge without separators (e.g. author emails and names on p1 of
  1706.03762v7).
- Table cells merge and exponents lose superscripts ("ModelBLEU", "2.3?10191.4?1020" when
  printed on a cp1252 console; the stored text keeps the original Unicode symbols).
- Some accented names split around the diacritic (2410.00037v1 p1).
Printing math symbols can fail on a cp1252 console; that is a console encoding issue,
not corrupted extraction. Run scripts/inspect_extraction.py to view samples.
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
    start_char: int      # character offset within pypdf-extracted page text
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


def chunk_pages(
    pages: list[tuple[int, str]],
    arxiv_version: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """Chunk already-extracted pages; chunk_size and overlap are in characters."""
    all_chunks: list[Chunk] = []
    for page_num, page_text in pages:
        all_chunks.extend(
            _split_page(page_text, arxiv_version, page_num, chunk_size, overlap, len(all_chunks))
        )
    return all_chunks


def extract_chunks(
    pdf_path: Path,
    arxiv_version: str,
    chunk_size: int,
    overlap: int,
) -> list[Chunk]:
    """Extract all chunks from one PDF; chunk_size and overlap are in characters."""
    return chunk_pages(extract_pages(pdf_path, arxiv_version), arxiv_version, chunk_size, overlap)
