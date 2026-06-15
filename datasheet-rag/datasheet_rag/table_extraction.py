"""Hybrid text-first table extraction for technical PDF datasheets."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

TABLE_PARSER_VERSION = "text-first-hybrid-v1"
TEXT_TABLE_PARSER = "native-text-geometry-v1"
VISUAL_TABLE_PARSER = "pillow-grid-native-words-v1"
TABLE_CROP_DPI = 200
BLACK_LINE_THRESHOLD = 80

PIN_HEADER_SIGNATURE = (
    "pin",
    "clock pin",
    "name",
    "function",
    "description",
    "dedicated function",
)
REGISTER_HEADERS = ["ID", "R/W", "Field", "Value ID", "Value", "Description"]


@dataclass(slots=True)
class ExtractedTable:
    """Canonical representation of one accepted table region."""

    page_number: int
    table_index: int
    table_title: str | None
    section_title: str | None
    headers: list[str | None]
    row_count: int
    column_count: int
    bbox: tuple[float, float, float, float]
    detection_source: str
    crop_path: str | None
    visual_parser: str
    native_bbox_text: str
    confidence_summary: dict[str, object]
    parser_family: str
    parser_mode: str
    table_kind: str
    header_signature: str
    region_sources: list[str]


@dataclass(slots=True)
class ExtractedTableRow:
    """Canonical representation of one accepted table row."""

    page_number: int
    table_index: int
    row_index: int
    chunk_type: str
    row_type: str
    table_title: str | None
    section_title: str | None
    headers: list[str | None]
    cells: list[str | None]
    visual_cells: list[str | None]
    native_fallback_text: str
    native_fallback_cells: list[str | None]
    text_rendering: str
    confidence_summary: dict[str, object]


@dataclass(slots=True)
class TableExtractionResult:
    """Collection of table extraction results for a document."""

    tables: list[ExtractedTable]
    rows: list[ExtractedTableRow]
    candidate_count: int


@dataclass(slots=True)
class TextLine:
    """Structured page text line for metadata inference."""

    text: str
    bbox: tuple[float, float, float, float]
    size: float
    color: int


@dataclass(slots=True)
class CandidateRegion:
    """Logical table region before canonical parsing."""

    page_number: int
    bbox: fitz.Rect
    detection_source: str
    nearby_table_title: str | None
    nearby_section_title: str | None
    parser_family: str
    parser_mode: str
    table_kind: str
    expected_column_count: int | None = None
    native_headers: list[str | None] | None = None
    native_rows: list[list[str | None]] | None = None
    region_sources: list[str] | None = None
    context_lines: list[str] | None = None


@dataclass(slots=True)
class _ParsedRow:
    """Internal parsed row before table storage."""

    row_type: str
    cells: list[str | None]
    visual_cells: list[str | None]
    native_fallback_text: str
    native_fallback_cells: list[str | None]
    confidence_summary: dict[str, object]


@dataclass(slots=True)
class _ParsedTable:
    """Internal parsed table payload."""

    headers: list[str | None]
    rows: list[_ParsedRow]
    crop_path: str | None
    native_bbox_text: str
    confidence_summary: dict[str, object]
    parser_name: str
    parser_family: str
    parser_mode: str
    table_kind: str


@dataclass(slots=True)
class _NativeTable:
    """Cached native PyMuPDF table extraction."""

    bbox: fitz.Rect
    col_count: int
    row_count: int
    headers: list[str | None]
    rows: list[list[str | None]]
    raw_rows: list[list[str | None]]


@dataclass(slots=True)
class _WordRow:
    """Grouped PDF words sharing approximately the same y band."""

    top: float
    bottom: float
    words: list[tuple[float, float, float, float, str, int, int, int]]


@dataclass(slots=True)
class _RowBand:
    """Visual row band in crop pixel coordinates."""

    top: int
    bottom: int
    words: list[tuple[float, float, float, float, str, int, int, int]]
    row_type: str


def extract_tables_from_document(
    document: fitz.Document,
    page_texts: dict[int, str],
    crop_dir: Path | None = None,
) -> TableExtractionResult:
    """Extract tables and row-level records from an already-open PDF document."""

    page_lines = {
        page_number: _extract_text_lines(document[page_number - 1])
        for page_number in range(1, document.page_count + 1)
    }
    return extract_tables_from_pages(
        document=document,
        page_numbers=list(range(1, document.page_count + 1)),
        page_lines=page_lines,
        crop_dir=crop_dir,
    )


def extract_tables_from_pages(
    *,
    document: fitz.Document,
    page_numbers: list[int],
    page_lines: dict[int, list[TextLine]],
    crop_dir: Path | None = None,
) -> TableExtractionResult:
    """Extract tables and rows for a specific set of pages."""

    if crop_dir is not None:
        crop_dir.mkdir(parents=True, exist_ok=True)

    tables: list[ExtractedTable] = []
    rows: list[ExtractedTableRow] = []
    candidate_count = 0

    for page_number in page_numbers:
        page = document[page_number - 1]
        native_tables = _extract_native_tables(page)
        candidates = _discover_candidates(
            page=page,
            page_number=page_number,
            page_lines=page_lines,
            native_tables=native_tables,
        )
        candidate_count += len(candidates)

        accepted_tables = 0
        for candidate in candidates:
            parsed = _parse_candidate_region(
                page=page,
                candidate=candidate,
                crop_dir=crop_dir,
            )
            if parsed is None:
                continue

            headers = parsed.headers
            row_records = parsed.rows
            table_index = accepted_tables
            accepted_tables += 1
            table_title = candidate.nearby_table_title
            section_title = candidate.nearby_section_title

            tables.append(
                ExtractedTable(
                    page_number=page_number,
                    table_index=table_index,
                    table_title=table_title,
                    section_title=section_title,
                    headers=headers,
                    row_count=len(row_records),
                    column_count=len(headers),
                    bbox=tuple(float(value) for value in candidate.bbox),
                    detection_source=candidate.detection_source,
                    crop_path=parsed.crop_path,
                    visual_parser=parsed.parser_name,
                    native_bbox_text=parsed.native_bbox_text,
                    confidence_summary=parsed.confidence_summary,
                    parser_family=parsed.parser_family,
                    parser_mode=parsed.parser_mode,
                    table_kind=parsed.table_kind,
                    header_signature=_header_signature(headers),
                    region_sources=list(candidate.region_sources or []),
                )
            )

            for row_index, row in enumerate(row_records):
                rows.append(
                    ExtractedTableRow(
                        page_number=page_number,
                        table_index=table_index,
                        row_index=row_index,
                        chunk_type="table_row",
                        row_type=row.row_type,
                        table_title=table_title,
                        section_title=section_title,
                        headers=headers,
                        cells=row.cells,
                        visual_cells=row.visual_cells,
                        native_fallback_text=row.native_fallback_text,
                        native_fallback_cells=row.native_fallback_cells,
                        text_rendering=_render_table_row(
                            page_number=page_number,
                            table_title=table_title,
                            section_title=section_title,
                            headers=headers,
                            cells=row.cells,
                            row_type=row.row_type,
                        ),
                        confidence_summary=row.confidence_summary,
                    )
                )

    return TableExtractionResult(tables=tables, rows=rows, candidate_count=candidate_count)


def _discover_candidates(
    *,
    page: fitz.Page,
    page_number: int,
    page_lines: dict[int, list[TextLine]],
    native_tables: list[_NativeTable],
) -> list[CandidateRegion]:
    """Build logical candidates from text anchors plus native table geometry."""

    candidates: list[CandidateRegion] = []
    candidates.extend(
        _discover_register_candidates(
            page=page,
            page_number=page_number,
            lines=page_lines[page_number],
            native_tables=native_tables,
            page_lines=page_lines,
        )
    )
    candidates.extend(
        _discover_pin_candidates(
            page=page,
            page_number=page_number,
            lines=page_lines[page_number],
            native_tables=native_tables,
            page_lines=page_lines,
        )
    )

    for native_table in native_tables:
        bbox = fitz.Rect(native_table.bbox)
        if any(_rect_overlap_ratio(bbox, existing.bbox) >= 0.7 for existing in candidates):
            continue
        candidates.append(
            CandidateRegion(
                page_number=page_number,
                bbox=bbox,
                detection_source="pymupdf_find_tables",
                nearby_table_title=_find_nearby_table_title(page_number, bbox, page_lines),
                nearby_section_title=_find_nearby_section_title(page_number, bbox, page_lines),
                parser_family="generic_native",
                parser_mode="text_first",
                table_kind="generic_text_table",
                expected_column_count=native_table.col_count,
                native_headers=native_table.headers,
                native_rows=native_table.raw_rows,
                region_sources=["pymupdf_find_tables"],
                context_lines=[],
            )
        )

    if not candidates:
        candidates.extend(_fallback_caption_candidates(page, page_number, page_lines[page_number]))

    return _dedupe_candidates(candidates)


def _extract_native_tables(page: fitz.Page) -> list[_NativeTable]:
    """Extract native PyMuPDF table results once per page."""

    native_tables: list[_NativeTable] = []
    for table in page.find_tables().tables:
        raw_rows = []
        for row in table.extract():
            raw_rows.append([_clean_cell(cell) for cell in row])
        headers = list(raw_rows[0]) if raw_rows else _extract_headers(table)
        native_tables.append(
            _NativeTable(
                bbox=fitz.Rect(table.bbox),
                col_count=table.col_count,
                row_count=table.row_count,
                headers=headers,
                rows=[[_normalize_table_cell(cell) for cell in row] for row in raw_rows],
                raw_rows=raw_rows,
            )
        )
    return native_tables


def _discover_register_candidates(
    *,
    page: fitz.Page,
    page_number: int,
    lines: list[TextLine],
    native_tables: list[_NativeTable],
    page_lines: dict[int, list[TextLine]],
) -> list[CandidateRegion]:
    """Create logical register-table candidates from repeated text anchors."""

    bit_indices = [index for index, line in enumerate(lines) if line.text == "Bit number"]
    candidates: list[CandidateRegion] = []
    for position, line_index in enumerate(bit_indices):
        bit_line = lines[line_index]
        next_bit_y = (
            lines[bit_indices[position + 1]].bbox[1]
            if position + 1 < len(bit_indices)
            else page.rect.y1 - 30
        )
        next_heading_index = None
        for candidate_index in range(line_index + 1, len(lines)):
            candidate_line = lines[candidate_index]
            if candidate_line.bbox[1] >= next_bit_y:
                break
            if _looks_like_numbered_heading(candidate_line.text):
                next_heading_index = candidate_index
                break
        end_y = (lines[next_heading_index].bbox[1] - 6) if next_heading_index is not None else (next_bit_y - 6)
        if end_y <= bit_line.bbox[1]:
            continue

        matching_native = [
            table
            for table in native_tables
            if table.bbox.y0 <= end_y + 8 and table.bbox.y1 >= bit_line.bbox[1] - 8
        ]
        if matching_native:
            bbox = fitz.Rect(matching_native[0].bbox)
            for table in matching_native[1:]:
                bbox.include_rect(table.bbox)
        else:
            bbox = _bbox_from_lines(
                page=page,
                lines=[line for line in lines if line.bbox[1] >= bit_line.bbox[1] and line.bbox[1] < end_y],
                padding=(4, 4, 4, 4),
            )

        section_title, context_lines = _find_register_context(lines, line_index)
        table_title = None
        if context_lines and context_lines[0].startswith("Address offset:"):
            table_title = context_lines[0]
            context_lines = context_lines[1:]

        if bbox is None:
            continue

        candidates.append(
            CandidateRegion(
                page_number=page_number,
                bbox=bbox,
                detection_source="text_anchor_register",
                nearby_table_title=table_title,
                nearby_section_title=section_title or _find_nearby_section_title(page_number, bbox, page_lines),
                parser_family="register",
                parser_mode="text_first",
                table_kind="register_table",
                expected_column_count=len(REGISTER_HEADERS),
                native_headers=REGISTER_HEADERS,
                native_rows=None,
                region_sources=["text_anchor_register", "pymupdf_find_tables"],
                context_lines=context_lines,
            )
        )
    return candidates


def _find_register_context(
    lines: list[TextLine],
    bit_line_index: int,
) -> tuple[str | None, list[str]]:
    """Capture heading and metadata lines immediately above a register block."""

    heading_index = None
    for index in range(bit_line_index - 1, -1, -1):
        text = lines[index].text
        if _looks_like_numbered_heading(text):
            heading_index = index
            break
        if lines[bit_line_index].bbox[1] - lines[index].bbox[3] > 120:
            break

    if heading_index is None:
        return None, []

    context_lines = []
    for index in range(heading_index + 1, bit_line_index):
        text = lines[index].text
        if text:
            context_lines.append(text)
    return lines[heading_index].text, context_lines


def _discover_pin_candidates(
    *,
    page: fitz.Page,
    page_number: int,
    lines: list[TextLine],
    native_tables: list[_NativeTable],
    page_lines: dict[int, list[TextLine]],
) -> list[CandidateRegion]:
    """Create pin-table candidates from PDF-native tables or text anchors."""

    candidates: list[CandidateRegion] = []
    matched_native = False
    for native_table in native_tables:
        if _looks_like_pin_headers(native_table.headers):
            matched_native = True
            bbox = fitz.Rect(native_table.bbox)
            candidates.append(
                CandidateRegion(
                    page_number=page_number,
                    bbox=bbox,
                    detection_source="pin_header_native",
                    nearby_table_title=_find_nearby_table_title(page_number, bbox, page_lines),
                    nearby_section_title=_find_nearby_section_title(page_number, bbox, page_lines),
                    parser_family="pin",
                    parser_mode="text_first",
                    table_kind="pin_table",
                    expected_column_count=native_table.col_count,
                    native_headers=native_table.headers,
                    native_rows=native_table.raw_rows,
                    region_sources=["pymupdf_find_tables", "pin_header_native"],
                    context_lines=[],
                )
            )
    if matched_native:
        return candidates

    header_line_indexes = [
        index
        for index, line in enumerate(lines)
        if line.text == "Pin"
    ]
    for line_index in header_line_indexes:
        window = "\n".join(item.text for item in lines[line_index: line_index + 8])
        if (
            "Function" not in window
            or "Description" not in window
            or "Clock" not in window
            or "Dedicated" not in window
        ):
            continue
        selected_lines = lines[line_index: line_index + 8]
        bbox = _bbox_from_lines(page=page, lines=selected_lines, padding=(4, 4, 440, 700))
        if bbox is None:
            continue
        bbox.y1 = min(page.rect.y1 - 30, bbox.y0 + 720)
        candidates.append(
            CandidateRegion(
                page_number=page_number,
                bbox=bbox,
                detection_source="pin_header_text",
                nearby_table_title=None,
                nearby_section_title=_find_nearby_section_title(page_number, bbox, page_lines),
                parser_family="pin",
                parser_mode="text_first",
                table_kind="pin_table",
                expected_column_count=6,
                native_headers=[header.title() for header in PIN_HEADER_SIGNATURE],
                native_rows=None,
                region_sources=["pin_header_text"],
                context_lines=[],
            )
        )
    return candidates


def _fallback_caption_candidates(
    page: fitz.Page,
    page_number: int,
    lines: list[TextLine],
) -> list[CandidateRegion]:
    """Create a text-driven fallback candidate when no table bbox is detected."""

    candidates: list[CandidateRegion] = []
    for index, line in enumerate(lines):
        if not (line.text.startswith("Table ") or _looks_like_section_heading_text(line.text)):
            continue

        caption_rect = fitz.Rect(line.bbox)
        gathered = [caption_rect]
        last_bottom = caption_rect.y1
        body_lines = 0
        for candidate_line in lines[index + 1:]:
            candidate_rect = fitz.Rect(candidate_line.bbox)
            if candidate_rect.y0 - last_bottom > 24:
                break
            if candidate_line.size >= line.size + 1.5 and body_lines > 0:
                break
            gathered.append(candidate_rect)
            last_bottom = candidate_rect.y1
            body_lines += 1
            if body_lines >= 6:
                break

        if body_lines < 3:
            continue

        region = fitz.Rect(gathered[0])
        for rect in gathered[1:]:
            region.include_rect(rect)
        region.x0 = max(page.rect.x0, region.x0 - 12)
        region.x1 = min(page.rect.x1, region.x1 + 220)
        region.y1 = min(page.rect.y1, region.y1 + 12)
        candidates.append(
            CandidateRegion(
                page_number=page_number,
                bbox=region,
                detection_source="caption_region_fallback",
                nearby_table_title=line.text if line.text.startswith("Table ") else None,
                nearby_section_title=None,
                parser_family="visual_fallback",
                parser_mode="visual_fallback",
                table_kind="visual_fallback_table",
                expected_column_count=None,
                native_headers=None,
                native_rows=None,
                region_sources=["caption_region_fallback"],
                context_lines=[],
            )
        )
    return candidates


def _dedupe_candidates(candidates: list[CandidateRegion]) -> list[CandidateRegion]:
    """Deduplicate overlapping candidate regions on the same page."""

    deduped: list[CandidateRegion] = []
    for candidate in sorted(candidates, key=lambda item: (item.page_number, item.bbox.y0, item.bbox.x0)):
        duplicate = False
        for existing in deduped:
            if existing.page_number != candidate.page_number:
                continue
            if _rect_overlap_ratio(existing.bbox, candidate.bbox) >= 0.85:
                duplicate = True
                break
        if not duplicate:
            deduped.append(candidate)
    return deduped


def _rect_overlap_ratio(a: fitz.Rect, b: fitz.Rect) -> float:
    """Compute overlap ratio over the smaller rectangle area."""

    intersection = fitz.Rect(a)
    intersection.intersect(b)
    if intersection.is_empty:
        return 0.0
    smaller_area = min(a.get_area(), b.get_area()) or 1.0
    return intersection.get_area() / smaller_area


def _bbox_from_lines(
    *,
    page: fitz.Page,
    lines: list[TextLine],
    padding: tuple[float, float, float, float],
) -> fitz.Rect | None:
    """Build a padded bounding box from a list of text lines."""

    if not lines:
        return None
    x0 = min(line.bbox[0] for line in lines) - padding[0]
    y0 = min(line.bbox[1] for line in lines) - padding[1]
    x1 = max(line.bbox[2] for line in lines) + padding[2]
    y1 = max(line.bbox[3] for line in lines) + padding[3]
    return fitz.Rect(
        max(page.rect.x0, x0),
        max(page.rect.y0, y0),
        min(page.rect.x1, x1),
        min(page.rect.y1, y1),
    )


def _parse_candidate_region(
    *,
    page: fitz.Page,
    candidate: CandidateRegion,
    crop_dir: Path | None,
) -> _ParsedTable | None:
    """Parse one candidate using its designated text or visual path."""

    if candidate.parser_family == "register":
        return _parse_register_candidate(page=page, candidate=candidate)
    if candidate.parser_family == "pin":
        return _parse_native_table_candidate(page=page, candidate=candidate, allow_visual_fallback=False, crop_dir=crop_dir)
    if candidate.parser_family == "visual_fallback":
        return _parse_visual_candidate(page=page, candidate=candidate, crop_dir=crop_dir)
    return _parse_native_table_candidate(page=page, candidate=candidate, allow_visual_fallback=True, crop_dir=crop_dir)


def _parse_register_candidate(
    *,
    page: fitz.Page,
    candidate: CandidateRegion,
) -> _ParsedTable | None:
    """Parse a register table using native text and word geometry."""

    words = page.get_text("words", clip=candidate.bbox, sort=True)
    if not words:
        return None

    rows = _group_words_into_rows(words)
    header_index = None
    for index, row in enumerate(rows):
        row_texts = [word[4] for word in row.words]
        if "R/W" in row_texts and "Field" in row_texts and "Description" in row_texts:
            header_index = index
            break
    if header_index is None:
        return None

    header_words = rows[header_index].words
    anchors = _register_column_anchors(header_words)
    if anchors is None:
        return None

    parsed_rows: list[_ParsedRow] = []
    pending_base: list[str | None] | None = None
    for row in rows[header_index + 1:]:
        cells = _cells_from_row_words(row.words, anchors)
        if not any(cells):
            continue

        main_present = any(cells[index] for index in range(3))
        tail_present = any(cells[index] for index in range(3, len(cells)))
        row_text = _normalize_bbox_text(_words_to_text(row.words))

        if main_present and tail_present:
            pending_base = cells[:3]
            parsed_rows.append(
                _ParsedRow(
                    row_type="data_row",
                    cells=cells,
                    visual_cells=list(cells),
                    native_fallback_text=row_text,
                    native_fallback_cells=list(cells),
                    confidence_summary={
                        "word_count": len(row.words),
                        "row_top": row.top,
                        "row_bottom": row.bottom,
                    },
                )
            )
            continue

        if main_present and not tail_present:
            pending_base = cells[:3]
            continue

        if tail_present and pending_base is not None:
            inherited = list(pending_base) + cells[3:]
            parsed_rows.append(
                _ParsedRow(
                    row_type="value_row",
                    cells=inherited,
                    visual_cells=list(inherited),
                    native_fallback_text=row_text,
                    native_fallback_cells=list(inherited),
                    confidence_summary={
                        "word_count": len(row.words),
                        "row_top": row.top,
                        "row_bottom": row.bottom,
                    },
                )
            )

    if not parsed_rows:
        return None

    context_text = " / ".join(candidate.context_lines or [])
    native_bbox_text = _normalize_bbox_text(
        " / ".join(
            part for part in [context_text, page.get_text("text", clip=candidate.bbox)] if part
        )
    )
    return _ParsedTable(
        headers=list(REGISTER_HEADERS),
        rows=parsed_rows,
        crop_path=None,
        native_bbox_text=native_bbox_text,
        confidence_summary={
            "row_count": len(parsed_rows),
            "strategy": "register_text_words",
        },
        parser_name=TEXT_TABLE_PARSER,
        parser_family="register",
        parser_mode="text_first",
        table_kind="register_table",
    )


def _group_words_into_rows(
    words: list[tuple[float, float, float, float, str, int, int, int]],
    tolerance: float = 4.0,
) -> list[_WordRow]:
    """Group native words into approximate text rows."""

    grouped: list[_WordRow] = []
    for word in sorted(words, key=lambda item: (((item[1] + item[3]) / 2), item[0])):
        top = word[1]
        bottom = word[3]
        center = (top + bottom) / 2
        if not grouped:
            grouped.append(_WordRow(top=top, bottom=bottom, words=[word]))
            continue
        last = grouped[-1]
        last_center = (last.top + last.bottom) / 2
        if abs(center - last_center) <= tolerance:
            last.words.append(word)
            last.top = min(last.top, top)
            last.bottom = max(last.bottom, bottom)
        else:
            grouped.append(_WordRow(top=top, bottom=bottom, words=[word]))
    for row in grouped:
        row.words.sort(key=lambda item: item[0])
    return grouped


def _register_column_anchors(
    words: list[tuple[float, float, float, float, str, int, int, int]],
) -> list[tuple[str, float, float]] | None:
    """Derive register-table column boundaries from the anchor header row."""

    items = sorted(words, key=lambda item: item[0])
    texts = [item[4] for item in items]
    try:
        id_anchor = next(item[0] for item in items if item[4] == "ID")
        rw_anchor = next(item[0] for item in items if item[4] == "R/W")
        field_anchor = next(item[0] for item in items if item[4] == "Field")
        description_anchor = next(item[0] for item in items if item[4] == "Description")
    except StopIteration:
        return None

    value_positions = [item[0] for item in items if item[4] == "Value"]
    if len(value_positions) < 2:
        return None
    value_id_anchor = value_positions[0]
    value_anchor = value_positions[1]

    anchors = [
        ("ID", id_anchor, (id_anchor + rw_anchor) / 2),
        ("R/W", rw_anchor, (rw_anchor + field_anchor) / 2),
        ("Field", field_anchor, (field_anchor + value_id_anchor) / 2),
        ("Value ID", value_id_anchor, (value_id_anchor + value_anchor) / 2),
        ("Value", value_anchor, (value_anchor + description_anchor) / 2),
        ("Description", description_anchor, float("inf")),
    ]
    return anchors


def _cells_from_row_words(
    words: list[tuple[float, float, float, float, str, int, int, int]],
    anchors: list[tuple[str, float, float]],
) -> list[str | None]:
    """Assign row words into canonical columns using x boundaries."""

    buckets = [[] for _ in anchors]
    for word in words:
        x_center = (word[0] + word[2]) / 2
        for index, (_name, _start, end) in enumerate(anchors):
            if x_center <= end:
                buckets[index].append(word)
                break
    return [_words_to_cell_text(bucket) for bucket in buckets]


def _parse_native_table_candidate(
    *,
    page: fitz.Page,
    candidate: CandidateRegion,
    allow_visual_fallback: bool,
    crop_dir: Path | None,
) -> _ParsedTable | None:
    """Parse a candidate from native PDF table rows."""

    if not candidate.native_rows:
        return None

    raw_rows = candidate.native_rows
    if not raw_rows:
        return None

    headers = [_normalize_table_cell(cell) for cell in raw_rows[0]]
    data_rows = raw_rows[1:]
    if candidate.parser_family == "generic_native" and _native_rows_need_visual_fallback(data_rows):
        if allow_visual_fallback:
            visual_candidate = replace(
                candidate,
                parser_family="visual_fallback",
                parser_mode="visual_fallback",
                table_kind="visual_fallback_table",
            )
            return _parse_visual_candidate(page=page, candidate=visual_candidate, crop_dir=crop_dir)
        return None

    parsed_rows: list[_ParsedRow] = []
    for row in data_rows:
        normalized_cells = [_normalize_table_cell(cell) for cell in row]
        if not any(normalized_cells):
            continue
        if candidate.parser_family == "pin" and not _pin_row_is_material(normalized_cells):
            continue
        row_type = _classify_native_row_type(normalized_cells)
        parsed_rows.append(
            _ParsedRow(
                row_type=row_type,
                cells=normalized_cells,
                visual_cells=list(normalized_cells),
                native_fallback_text=" / ".join(cell for cell in normalized_cells if cell),
                native_fallback_cells=list(normalized_cells),
                confidence_summary={
                    "filled_cell_count": len([cell for cell in normalized_cells if cell]),
                    "strategy": f"{candidate.parser_family}_native_table",
                },
            )
        )

    if not parsed_rows:
        return None

    return _ParsedTable(
        headers=headers,
        rows=parsed_rows,
        crop_path=None,
        native_bbox_text=_normalize_bbox_text(page.get_text("text", clip=candidate.bbox)),
        confidence_summary={
            "row_count": len(parsed_rows),
            "strategy": f"{candidate.parser_family}_native_table",
        },
        parser_name=TEXT_TABLE_PARSER,
        parser_family=candidate.parser_family,
        parser_mode="text_first",
        table_kind=candidate.table_kind,
    )


def _native_rows_need_visual_fallback(rows: list[list[str | None]]) -> bool:
    """Decide whether native row extraction is too merged to trust canonically."""

    for row in rows:
        newline_counts = [
            cell.count("\n") + 1
            for cell in row
            if isinstance(cell, str) and "\n" in cell and cell.strip()
        ]
        if len(newline_counts) >= 2 and max(newline_counts) > 1:
            return True
    return False


def _pin_row_is_material(cells: list[str | None]) -> bool:
    """Skip pin-table continuation noise that lacks row identity."""

    if cells[0]:
        return True
    if cells[2]:
        return True
    return False


def _classify_native_row_type(cells: list[str | None]) -> str:
    """Classify simple text-first rows."""

    non_empty_indices = [index for index, cell in enumerate(cells) if cell]
    if non_empty_indices == [0]:
        return "group_header"
    return "data_row"


def _normalize_table_cell(value: str | None) -> str | None:
    """Normalize multiline native table cells for canonical storage."""

    if value is None:
        return None
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    if not lines:
        return None
    return " / ".join(lines)


def _parse_visual_candidate(
    *,
    page: fitz.Page,
    candidate: CandidateRegion,
    crop_dir: Path | None,
) -> _ParsedTable | None:
    """Render, visually segment, and reconcile one table candidate."""

    crop_image, scale = _render_crop(page, candidate.bbox)
    crop_array = np.array(crop_image)
    horizontal_bands = _detect_separator_bands(crop_array, axis="horizontal")
    vertical_bands = _detect_separator_bands(crop_array, axis="vertical")
    row_bands = _build_row_bands(page, candidate.bbox, scale, horizontal_bands)
    if not row_bands:
        return None

    column_bounds = _build_column_bounds(
        page,
        candidate.bbox,
        scale,
        vertical_bands,
        row_bands,
        candidate.expected_column_count,
    )
    if len(column_bounds) < 2:
        return None

    row_structures = _extract_row_structures(
        page=page,
        bbox=candidate.bbox,
        scale=scale,
        row_bands=row_bands,
        column_bounds=column_bounds,
        crop_array=crop_array,
    )
    if not row_structures:
        return None

    headers, parsed_rows = _split_header_row(row_structures, candidate.native_headers)
    if not headers:
        headers = [f"Column {index + 1}" for index in range(len(column_bounds))]

    crop_path = None
    if crop_dir is not None:
        bbox_label = "_".join(str(int(round(value * 10))) for value in candidate.bbox)
        crop_path = str((crop_dir / f"page_{candidate.page_number:04d}_{bbox_label}.png").resolve())
        crop_image.save(crop_path)

    return _ParsedTable(
        headers=headers,
        rows=[row for row in parsed_rows if row.row_type != "header_row"],
        crop_path=crop_path,
        native_bbox_text=_normalize_bbox_text(page.get_text("text", clip=candidate.bbox)),
        confidence_summary={
            "row_band_count": len(row_bands),
            "column_count": len(column_bounds),
            "horizontal_separator_count": len(horizontal_bands),
            "vertical_separator_count": len(vertical_bands),
        },
        parser_name=VISUAL_TABLE_PARSER,
        parser_family="visual_fallback",
        parser_mode="visual_fallback",
        table_kind="visual_fallback_table",
    )


def _render_crop(page: fitz.Page, bbox: fitz.Rect) -> tuple[Image.Image, float]:
    """Render a table candidate crop at a fixed DPI."""

    scale = TABLE_CROP_DPI / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=bbox, alpha=False)
    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
    return image, scale


def _detect_separator_bands(crop_array: np.ndarray, axis: str) -> list[tuple[int, int]]:
    """Detect dark separator bands from the rendered crop."""

    dark_mask = np.all(crop_array < BLACK_LINE_THRESHOLD, axis=2)
    fractions = dark_mask.mean(axis=1 if axis == "horizontal" else 0)
    threshold = 0.45 if axis == "horizontal" else 0.5
    indices = [index for index, value in enumerate(fractions) if value >= threshold]
    bands = _group_contiguous(indices)
    length = crop_array.shape[0] if axis == "horizontal" else crop_array.shape[1]
    if not bands:
        return [(0, 0), (length - 1, length - 1)]
    if bands[0][0] > 2:
        bands.insert(0, (0, 0))
    if bands[-1][1] < length - 3:
        bands.append((length - 1, length - 1))
    return bands


def _group_contiguous(indices: list[int]) -> list[tuple[int, int]]:
    """Group contiguous indices into closed intervals."""

    if not indices:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        groups.append((start, previous))
        start = previous = index
    groups.append((start, previous))
    return groups


def _build_row_bands(
    page: fitz.Page,
    bbox: fitz.Rect,
    scale: float,
    horizontal_bands: list[tuple[int, int]],
) -> list[_RowBand]:
    """Create visual row bands from separator gaps plus native words."""

    words = page.get_text("words", clip=bbox, sort=True)
    if not words:
        return []

    row_intervals: list[tuple[int, int]] = []
    for start, end in horizontal_bands:
        row_intervals.append((start, end))
    for first, second in zip(horizontal_bands, horizontal_bands[1:]):
        gap_top = first[1] + 1
        gap_bottom = second[0] - 1
        if gap_bottom >= gap_top:
            row_intervals.append((gap_top, gap_bottom))
    row_intervals = sorted(set(row_intervals))

    row_bands: list[_RowBand] = []
    for top, bottom in row_intervals:
        interval_words = [
            word
            for word in words
            if top <= _word_center_to_pixel(word, bbox.y0, scale, axis="y") <= bottom
        ]
        if not interval_words:
            continue
        row_bands.append(
            _RowBand(
                top=top,
                bottom=bottom,
                words=interval_words,
                row_type="unknown",
            )
        )
    return row_bands


def _build_column_bounds(
    page: fitz.Page,
    bbox: fitz.Rect,
    scale: float,
    vertical_bands: list[tuple[int, int]],
    row_bands: list[_RowBand],
    expected_column_count: int | None,
) -> list[tuple[int, int]]:
    """Build column boundaries from dark separator bands or header cells."""

    if len(vertical_bands) >= 2:
        bounds: list[tuple[int, int]] = []
        previous_end = 0
        for start, end in vertical_bands[1:]:
            bounds.append((previous_end, start))
            previous_end = end
        bounds[-1] = (bounds[-1][0], vertical_bands[-1][0])
        normalized = [(left, right) for left, right in bounds if right - left > 4]
        if normalized:
            return _reconcile_column_bounds(normalized, expected_column_count)

    header_words = row_bands[0].words
    x_positions = sorted(
        {
            int(round(_word_left_to_pixel(word, bbox.x0, scale)))
            for word in header_words
        }
    )
    if len(x_positions) < 2:
        return []

    separators = [0]
    for left, right in zip(x_positions, x_positions[1:]):
        if right - left > 40:
            separators.append((left + right) // 2)
    separators.append(int(round((bbox.x1 - bbox.x0) * scale)))
    fallback_bounds = [
        (separators[index], separators[index + 1])
        for index in range(len(separators) - 1)
        if separators[index + 1] - separators[index] > 4
    ]
    return _reconcile_column_bounds(fallback_bounds, expected_column_count)


def _extract_row_structures(
    *,
    page: fitz.Page,
    bbox: fitz.Rect,
    scale: float,
    row_bands: list[_RowBand],
    column_bounds: list[tuple[int, int]],
    crop_array: np.ndarray,
) -> list[_ParsedRow]:
    """Extract canonical row/cell structures using visual row/column geometry."""

    rows: list[_ParsedRow] = []
    for row_band in row_bands:
        visual_cells: list[str | None] = []
        native_fallback_cells: list[str | None] = []
        for left, right in column_bounds:
            cell_rect = fitz.Rect(
                bbox.x0 + (left / scale),
                bbox.y0 + (row_band.top / scale),
                bbox.x0 + (right / scale),
                bbox.y0 + ((row_band.bottom + 1) / scale),
            )
            cell_words = page.get_text("words", clip=cell_rect, sort=True)
            cell_text = _words_to_cell_text(cell_words)
            visual_cells.append(cell_text)
            native_fallback_cells.append(_normalize_bbox_text(page.get_text("text", clip=cell_rect)) or None)

        non_empty_cells = [cell for cell in visual_cells if cell]
        if not non_empty_cells:
            continue

        native_fallback_text = _normalize_bbox_text(
            page.get_text(
                "text",
                clip=fitz.Rect(
                    bbox.x0,
                    bbox.y0 + (row_band.top / scale),
                    bbox.x1,
                    bbox.y0 + ((row_band.bottom + 1) / scale),
                ),
            )
        )
        row_type = _classify_row_type(
            visual_cells=visual_cells,
            crop_array=crop_array,
            row_band=row_band,
        )
        rows.append(
            _ParsedRow(
                row_type=row_type,
                cells=list(visual_cells),
                visual_cells=list(visual_cells),
                native_fallback_text=native_fallback_text,
                native_fallback_cells=native_fallback_cells,
                confidence_summary={
                    "filled_cell_count": len(non_empty_cells),
                    "native_word_count": len(row_band.words),
                    "row_top": row_band.top,
                    "row_bottom": row_band.bottom,
                },
            )
        )
    return rows


def _split_header_row(
    rows: list[_ParsedRow],
    native_headers: list[str | None] | None,
) -> tuple[list[str | None], list[_ParsedRow]]:
    """Use the first row as headers when it clearly behaves like a header row."""

    if not rows:
        return [], []
    first_row = rows[0]
    filled_cells = [cell for cell in first_row.cells if cell]
    if len(filled_cells) >= 2:
        first_row.row_type = "header_row"
        headers = [cell if cell else None for cell in first_row.cells]
        if native_headers and len(native_headers) == len(headers):
            headers = list(native_headers)
        return headers, rows
    if native_headers:
        return list(native_headers), rows
    return [f"Column {index + 1}" for index in range(len(first_row.cells))], rows


def _reconcile_column_bounds(
    bounds: list[tuple[int, int]],
    expected_column_count: int | None,
) -> list[tuple[int, int]]:
    """Merge visually over-split columns back to the expected count."""

    if expected_column_count is None or len(bounds) <= expected_column_count:
        return bounds

    merged = list(bounds)
    while len(merged) > expected_column_count:
        widths = [right - left for left, right in merged]
        smallest_index = widths.index(min(widths))
        if smallest_index == 0:
            merge_pair = (0, 1)
        elif smallest_index == len(merged) - 1:
            merge_pair = (smallest_index - 1, smallest_index)
        else:
            left_index = smallest_index - 1
            right_index = smallest_index + 1
            if widths[left_index] <= widths[right_index]:
                merge_pair = (left_index, smallest_index)
            else:
                merge_pair = (smallest_index, right_index)
        left_index, right_index = merge_pair
        merged[left_index:right_index + 1] = [
            (merged[left_index][0], merged[right_index][1])
        ]
    return merged


def _classify_row_type(
    *,
    visual_cells: list[str | None],
    crop_array: np.ndarray,
    row_band: _RowBand,
) -> str:
    """Classify a row as data, group header, or unknown."""

    non_empty_indices = [index for index, cell in enumerate(visual_cells) if cell]
    if not non_empty_indices:
        return "unknown"
    if non_empty_indices == [0]:
        return "group_header"

    region = crop_array[max(row_band.top, 0): min(row_band.bottom + 1, crop_array.shape[0]), :, :]
    if region.size:
        mean_color = region.mean(axis=(0, 1))
        if mean_color[1] > mean_color[0] + 15 and mean_color[1] > mean_color[2] + 10 and non_empty_indices == [0]:
            return "group_header"
    return "data_row"


def _word_center_to_pixel(
    word: tuple[float, float, float, float, str, int, int, int],
    origin: float,
    scale: float,
    *,
    axis: str,
) -> int:
    """Convert a word center to crop pixel coordinates."""

    if axis == "y":
        center = (word[1] + word[3]) / 2
    else:
        center = (word[0] + word[2]) / 2
    return int(round((center - origin) * scale))


def _word_left_to_pixel(
    word: tuple[float, float, float, float, str, int, int, int],
    origin: float,
    scale: float,
) -> float:
    """Convert a word left edge to crop pixel coordinates."""

    return (word[0] - origin) * scale


def _words_to_cell_text(
    words: list[tuple[float, float, float, float, str, int, int, int]],
) -> str | None:
    """Convert native PDF words into normalized cell text."""

    if not words:
        return None
    lines: list[list[str]] = []
    line_centers: list[float] = []
    for word in sorted(words, key=lambda item: (((item[1] + item[3]) / 2), item[0])):
        center = (word[1] + word[3]) / 2
        if not line_centers or abs(center - line_centers[-1]) > 3.0:
            lines.append([word[4]])
            line_centers.append(center)
            continue
        lines[-1].append(word[4])
    normalized_lines = [" ".join(line).strip() for line in lines if line]
    text = " / ".join(line for line in normalized_lines if line)
    return text or None


def _words_to_text(
    words: list[tuple[float, float, float, float, str, int, int, int]],
) -> str:
    """Render a list of words back into readable line text."""

    lines: list[list[str]] = []
    line_centers: list[float] = []
    for word in sorted(words, key=lambda item: (((item[1] + item[3]) / 2), item[0])):
        center = (word[1] + word[3]) / 2
        if not line_centers or abs(center - line_centers[-1]) > 3.0:
            lines.append([word[4]])
            line_centers.append(center)
            continue
        lines[-1].append(word[4])
    return "\n".join(" ".join(line).strip() for line in lines if line)


def _render_table_row(
    *,
    page_number: int,
    table_title: str | None,
    section_title: str | None,
    headers: list[str | None],
    cells: list[str | None],
    row_type: str,
) -> str:
    """Build a search-friendly text rendering from structured row cells."""

    parts = [f"Page {page_number}."]
    if section_title:
        parts.append(f"Section: {section_title}.")
    if table_title:
        parts.append(f"Table: {table_title}.")
    if row_type == "group_header" and cells and cells[0]:
        parts.append(f"Group: {cells[0]}.")
        return " ".join(parts).strip()

    labeled_cells: list[str] = []
    for index, cell in enumerate(cells):
        if not cell:
            continue
        header = headers[index] if index < len(headers) else None
        if header:
            labeled_cells.append(f"{header}: {cell}")
        else:
            labeled_cells.append(cell)
    if labeled_cells:
        parts.append(" ".join(f"{item}." for item in labeled_cells))
    return " ".join(parts).strip()


def _extract_text_lines(page: fitz.Page) -> list[TextLine]:
    """Extract structured text lines with style metadata for region-aware inference."""

    text = page.get_text("dict")
    lines: list[TextLine] = []
    for block in text["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = line["spans"]
            line_text = "".join(span["text"] for span in spans).strip()
            if not line_text:
                continue
            lines.append(
                TextLine(
                    text=line_text,
                    bbox=(
                        min(span["bbox"][0] for span in spans),
                        min(span["bbox"][1] for span in spans),
                        max(span["bbox"][2] for span in spans),
                        max(span["bbox"][3] for span in spans),
                    ),
                    size=max(span["size"] for span in spans),
                    color=spans[0]["color"],
                )
            )
    return lines


def _find_nearby_section_title(
    page_number: int,
    bbox: fitz.Rect,
    page_lines: dict[int, list[TextLine]],
) -> str | None:
    """Infer a human-visible section title above a specific table region."""

    lines = page_lines.get(page_number, [])
    candidates: list[tuple[float, str]] = []
    for line in lines:
        line_rect = fitz.Rect(line.bbox)
        if line_rect.y1 >= bbox.y0:
            continue
        if bbox.y0 - line_rect.y1 > 120:
            continue
        if line.text.startswith("Table "):
            continue
        if _looks_like_noise_title(line.text):
            continue
        score = 0.0
        distance = bbox.y0 - line_rect.y1
        score -= distance
        score += line.size * 4
        if line.color != 0:
            score += 10
        if len(line.text) <= 60:
            score += 8
        if _looks_like_section_heading_text(line.text):
            score += 12
        candidates.append((score, line.text))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    for lookback_page in range(page_number, max(0, page_number - 3), -1):
        for line in page_lines.get(lookback_page, []):
            if _looks_like_section_heading_text(line.text):
                return line.text
    return None


def _find_nearby_table_title(
    page_number: int,
    bbox: fitz.Rect,
    page_lines: dict[int, list[TextLine]],
) -> str | None:
    """Infer a table caption only when one is actually near the region."""

    lines = page_lines.get(page_number, [])
    candidates: list[tuple[float, str]] = []
    for line in lines:
        if not line.text.startswith("Table "):
            continue
        line_rect = fitz.Rect(line.bbox)
        vertical_distance = min(abs(bbox.y0 - line_rect.y1), abs(line_rect.y0 - bbox.y1))
        if vertical_distance > 80:
            continue
        score = -vertical_distance + line.size * 2
        candidates.append((score, line.text))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _looks_like_noise_title(text: str) -> bool:
    """Filter out obviously bad metadata candidates such as numeric values."""

    if re.fullmatch(r"[\d\.\+\-]+\s*(?:mA|μA|uA|V|KB|MHz)?", text):
        return True
    return False


def _looks_like_section_heading_text(text: str) -> bool:
    """Recognize section-style lines or short display headings."""

    if re.match(r"^\d+(?:\.\d+){1,}\s+\S.*$", text):
        return True
    return len(text) <= 50 and text[:1].isupper()


def _looks_like_numbered_heading(text: str) -> bool:
    """Recognize numbered section headings only."""

    return re.match(r"^\d+(?:\.\d+){1,}\s+\S.*$", text) is not None


def _normalize_bbox_text(text: str) -> str:
    """Normalize clipped native PDF text for debug storage."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines)


def _extract_headers(table: object) -> list[str | None]:
    """Extract native header labels from a PyMuPDF table when available."""

    header = getattr(table, "header", None)
    names = getattr(header, "names", None)
    if names:
        return [_clean_cell(name) for name in names]

    extracted = getattr(table, "extract", lambda: [])()
    if extracted:
        return [_clean_cell(cell) for cell in extracted[0]]
    return []


def _clean_cell(value: object) -> str | None:
    """Normalize a raw table cell value for stable storage."""

    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _looks_like_pin_headers(headers: list[str | None]) -> bool:
    """Recognize the pin-assignment header family."""

    normalized = [
        "" if header is None else re.sub(r"\s+", " ", header).strip().lower()
        for header in headers
    ]
    if len(normalized) < 6:
        return False
    return tuple(normalized[:6]) == PIN_HEADER_SIGNATURE


def _header_signature(headers: list[str | None]) -> str:
    """Build a stable compact signature for debug and storage."""

    return " | ".join(
        "" if header is None else re.sub(r"\s+", " ", header).strip()
        for header in headers
    )
