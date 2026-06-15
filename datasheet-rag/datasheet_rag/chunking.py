"""Page-aware chunking for extracted PDF prose."""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 900


@dataclass(slots=True)
class ChunkRecord:
    """Canonical chunk representation for prose retrieval."""

    page_number: int
    chunk_index: int
    chunk_type: str
    source_text: str


def chunk_pages(page_texts: list[tuple[int, str]], max_chars: int = DEFAULT_MAX_CHARS) -> list[ChunkRecord]:
    """Create page-bounded text chunks from extracted page text."""

    chunks: list[ChunkRecord] = []
    for page_number, page_text in page_texts:
        for chunk in chunk_page(page_number=page_number, page_text=page_text, max_chars=max_chars):
            chunks.append(chunk)
    return chunks


def chunk_page(page_number: int, page_text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[ChunkRecord]:
    """Split one page of text into stable chunks without crossing page boundaries."""

    normalized = page_text.replace("\r\n", "\n").strip()
    if not normalized:
        return []

    units = _prepare_units(normalized, max_chars=max_chars)
    chunk_texts: list[str] = []
    current_units: list[str] = []
    current_length = 0

    for unit in units:
        separator_length = 2 if current_units else 0
        projected_length = current_length + separator_length + len(unit)
        if current_units and projected_length > max_chars:
            chunk_texts.append("\n\n".join(current_units).strip())
            current_units = [unit]
            current_length = len(unit)
            continue
        current_units.append(unit)
        current_length = projected_length

    if current_units:
        chunk_texts.append("\n\n".join(current_units).strip())

    return [
        ChunkRecord(
            page_number=page_number,
            chunk_index=index,
            chunk_type="text_chunk",
            source_text=chunk_text,
        )
        for index, chunk_text in enumerate(chunk_texts)
        if chunk_text
    ]


def _prepare_units(page_text: str, max_chars: int) -> list[str]:
    """Break a page into chunkable units, preserving paragraph structure where possible."""

    raw_units = [unit.strip() for unit in re.split(r"\n\s*\n", page_text) if unit.strip()]
    if not raw_units:
        raw_units = [page_text.strip()]

    units: list[str] = []
    for raw_unit in raw_units:
        if len(raw_unit) <= max_chars:
            units.append(raw_unit)
            continue
        units.extend(_split_large_unit(raw_unit, max_chars=max_chars))
    return units


def _split_large_unit(text: str, max_chars: int) -> list[str]:
    """Split long units on sentences first, then lines, then hard-wrap as a fallback."""

    sentence_parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        if part.strip()
    ]
    if len(sentence_parts) > 1:
        return _merge_small_parts(sentence_parts, max_chars=max_chars)

    line_parts = [part.strip() for part in text.splitlines() if part.strip()]
    if len(line_parts) > 1:
        return _merge_small_parts(line_parts, max_chars=max_chars)

    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current_words: list[str] = []
    current_length = 0
    for word in words:
        separator_length = 1 if current_words else 0
        projected_length = current_length + separator_length + len(word)
        if current_words and projected_length > max_chars:
            chunks.append(" ".join(current_words))
            current_words = [word]
            current_length = len(word)
            continue
        current_words.append(word)
        current_length = projected_length
    if current_words:
        chunks.append(" ".join(current_words))
    return chunks


def _merge_small_parts(parts: list[str], max_chars: int) -> list[str]:
    """Combine small sentence or line fragments into target-sized units."""

    merged: list[str] = []
    current_parts: list[str] = []
    current_length = 0
    for part in parts:
        separator_length = 1 if current_parts else 0
        projected_length = current_length + separator_length + len(part)
        if current_parts and projected_length > max_chars:
            merged.append(" ".join(current_parts))
            current_parts = [part]
            current_length = len(part)
            continue
        current_parts.append(part)
        current_length = projected_length
    if current_parts:
        merged.append(" ".join(current_parts))
    return merged
