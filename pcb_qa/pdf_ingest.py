from __future__ import annotations

import re
from pathlib import Path

from .utils import tokenize, write_json, write_jsonl

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None


MAX_CHUNK_CHARS = 1800
CHUNK_OVERLAP_CHARS = 250


def _extract_pdf_pages(pdf_path: Path) -> list[str]:
    if PdfReader is None:
        raise RuntimeError(
            "pypdf is required for PDF ingestion. Install dependencies from requirements.txt."
        )
    reader = PdfReader(str(pdf_path))
    return [page.extract_text() or "" for page in reader.pages]


def _sheet_hint(text: str) -> str:
    patterns = [r"([A-Za-z0-9_]+\.SchDoc)", r"--\s*\d+\s+of\s+\d+\s*--"]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return "unknown_sheet"


def _find_heading_boundaries(text: str) -> list[int]:
    boundaries = [0]
    lines = text.splitlines()
    offset = 0
    for line in lines:
        normalized = line.strip()
        is_heading = (
            3 <= len(normalized) <= 80
            and (
                normalized.isupper()
                or re.match(r"^\d+(\.\d+)*\s+[A-Za-z]", normalized)
                or normalized.lower().startswith(("table ", "figure ", "section "))
            )
        )
        if is_heading:
            boundaries.append(offset)
        offset += len(line) + 1
    boundaries.append(len(text))
    boundaries = sorted(set(boundaries))
    return boundaries


def _chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    boundaries = _find_heading_boundaries(text)
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        target_end = min(cursor + MAX_CHUNK_CHARS, len(text))
        candidate = [b for b in boundaries if cursor < b <= target_end]
        end = candidate[-1] if candidate else target_end
        if end <= cursor:
            # Defensive forward progress for malformed boundaries.
            end = min(cursor + MAX_CHUNK_CHARS, len(text))
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_cursor = max(0, end - CHUNK_OVERLAP_CHARS)
        if next_cursor <= cursor:
            next_cursor = end
        cursor = next_cursor
    return chunks


def build_pdf_chunks(
    schematic_pdf: Path, resources_dir: Path, output_dir: Path
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_rows: list[dict] = []

    schematic_pages = _extract_pdf_pages(schematic_pdf)
    for page_idx, page_text in enumerate(schematic_pages, start=1):
        sheet_name = _sheet_hint(page_text)
        chunks = _chunk_text(page_text)
        for chunk_idx, chunk in enumerate(chunks, start=1):
            chunk_rows.append(
                {
                    "chunk_id": f"schematic:{page_idx}:{chunk_idx}",
                    "source_type": "schematic",
                    "source_file": schematic_pdf.name,
                    "page_start": page_idx,
                    "page_end": page_idx,
                    "heading_path": [sheet_name],
                    "tokens": tokenize(chunk),
                    "text": chunk,
                }
            )

    datasheet_files = sorted(resources_dir.glob("*.pdf"))
    for pdf in datasheet_files:
        pages = _extract_pdf_pages(pdf)
        for page_idx, page_text in enumerate(pages, start=1):
            chunks = _chunk_text(page_text)
            if not chunks:
                continue
            for chunk_idx, chunk in enumerate(chunks, start=1):
                heading = _sheet_hint(chunk)
                part_candidates = [pdf.stem, pdf.name]
                chunk_rows.append(
                    {
                        "chunk_id": f"datasheet:{pdf.stem}:{page_idx}:{chunk_idx}",
                        "source_type": "datasheet",
                        "source_file": pdf.name,
                        "page_start": page_idx,
                        "page_end": page_idx,
                        "heading_path": [heading],
                        "part_number_candidates": part_candidates,
                        "tokens": tokenize(chunk),
                        "text": chunk,
                    }
                )

    write_jsonl(output_dir / "pdf_chunks.jsonl", chunk_rows)
    write_json(
        output_dir / "pdf_chunk_manifest.json",
        {
            "schematic_pdf": schematic_pdf.name,
            "datasheet_files": [f.name for f in datasheet_files],
            "chunk_count": len(chunk_rows),
        },
    )
    return {"pdf_chunk_count": len(chunk_rows), "datasheet_files": len(datasheet_files)}

