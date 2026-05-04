#!/usr/bin/env python3
"""Normalization and deterministic heuristics for datasheet extraction."""

from __future__ import annotations

import re
from statistics import median
from typing import Any, Dict, Iterable, List, Tuple


SECTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("features", re.compile(r"\bfeatures?\b", re.IGNORECASE)),
    ("applications", re.compile(r"\bapplications?\b", re.IGNORECASE)),
    ("description", re.compile(r"\b(description|overview)\b", re.IGNORECASE)),
    ("pin_configuration", re.compile(r"\bpin\s+configuration\b", re.IGNORECASE)),
    ("pin_description", re.compile(r"\bpin\s+description\b", re.IGNORECASE)),
    ("absolute_maximum_ratings", re.compile(r"\babsolute\s+maximum\s+ratings?\b", re.IGNORECASE)),
    ("recommended_operating_conditions", re.compile(r"\brecommended\s+operating\s+conditions?\b", re.IGNORECASE)),
    ("electrical_characteristics", re.compile(r"\belectrical\s+characteristics?\b", re.IGNORECASE)),
    ("timing_characteristics", re.compile(r"\btiming\s+characteristics?\b", re.IGNORECASE)),
    ("typical_application", re.compile(r"\btypical\s+application\b", re.IGNORECASE)),
    ("layout_guidelines", re.compile(r"\blayout\s+guidelines?\b", re.IGNORECASE)),
    ("package_information", re.compile(r"\bpackage\s+information\b", re.IGNORECASE)),
    ("ordering_information", re.compile(r"\bordering\s+information\b", re.IGNORECASE)),
    ("revision_history", re.compile(r"\brevision\s+history\b", re.IGNORECASE)),
]

PIN_HEADER_ALIASES = {
    "pin": {"pin", "pin no", "pin number", "ball", "pad"},
    "name": {"name", "signal", "signal name", "pin name"},
    "type": {"type", "i/o", "io", "dir", "direction"},
    "description": {"description", "function", "details"},
}


def _simple_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _is_heading_text(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return False
    if len(compact) > 140:
        return False
    if compact.endswith(":"):
        return True
    if re.match(r"^\d+(\.\d+)*\s+\S+", compact):
        return True
    alpha = re.sub(r"[^A-Za-z]", "", compact)
    return bool(alpha) and alpha.isupper() and len(alpha) > 3


def _detect_repeated_edge_text(
    pages_raw: List[Dict[str, Any]],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    top_counts: Dict[str, int] = {}
    bottom_counts: Dict[str, int] = {}
    for page in pages_raw:
        height = float(page.get("height", 0.0))
        for block in page.get("text_blocks_raw", []):
            text_key = _simple_text(block.get("text", ""))
            if not text_key:
                continue
            bbox = block.get("bbox") or [0, 0, 0, 0]
            y0 = float(bbox[1])
            y1 = float(bbox[3])
            if y0 <= height * 0.12:
                top_counts[text_key] = top_counts.get(text_key, 0) + 1
            if y1 >= height * 0.88:
                bottom_counts[text_key] = bottom_counts.get(text_key, 0) + 1
    return top_counts, bottom_counts


def _normalize_header_names(columns: Iterable[str]) -> Dict[int, str]:
    normalized: Dict[int, str] = {}
    for idx, header in enumerate(columns):
        key = _simple_text(header)
        for canonical, aliases in PIN_HEADER_ALIASES.items():
            if key in aliases:
                normalized[idx] = canonical
                break
    return normalized


def _build_pin_table_payload(table: Dict[str, Any]) -> Dict[str, Any]:
    columns = list(table.get("columns", []))
    mapping = _normalize_header_names(columns)
    rows_payload: List[Dict[str, str]] = []
    for row in table.get("rows", []):
        row_payload: Dict[str, str] = {}
        for idx, value in enumerate(row):
            if idx in mapping:
                row_payload[mapping[idx]] = value
        if row_payload:
            rows_payload.append(row_payload)
    return {
        "page": int(table.get("page_number", 0)),
        "columns": columns,
        "rows": rows_payload,
        "table_id": table.get("table_id", ""),
    }


def _classify_block(
    block: Dict[str, Any],
    *,
    page_height: float,
    median_font_size: float,
    top_counts: Dict[str, int],
    bottom_counts: Dict[str, int],
    total_pages: int,
) -> str:
    text = str(block.get("text", "")).strip()
    text_key = _simple_text(text)
    bbox = block.get("bbox") or [0, 0, 0, 0]
    y0 = float(bbox[1])
    y1 = float(bbox[3])
    font_size = float(block.get("font_size_max", 0.0))
    is_bold = bool(block.get("is_bold", False))

    if text_key and top_counts.get(text_key, 0) >= max(2, total_pages // 2) and y0 <= page_height * 0.12:
        return "header"
    if text_key and bottom_counts.get(text_key, 0) >= max(2, total_pages // 2) and y1 >= page_height * 0.88:
        return "footer"

    if re.match(r"^(table|figure)\s+\d+", text, re.IGNORECASE):
        return "caption"
    if font_size >= (median_font_size + 1.2) or (is_bold and _is_heading_text(text)):
        return "heading"
    if "|" in text and len(text.split("|")) >= 3:
        return "table"
    if text:
        return "paragraph"
    return "unknown"


def normalize_document(raw: Dict[str, Any], table_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build normalized document representation used for markdown rendering."""
    pages_raw = list(raw.get("pages", []))
    total_pages = len(pages_raw)
    top_counts, bottom_counts = _detect_repeated_edge_text(pages_raw)
    all_font_sizes = [
        float(block.get("font_size_max", 0.0))
        for page in pages_raw
        for block in page.get("text_blocks_raw", [])
        if float(block.get("font_size_max", 0.0)) > 0.0
    ]
    median_font_size = median(all_font_sizes) if all_font_sizes else 10.0

    normalized_pages: List[Dict[str, Any]] = []
    detected_sections: List[Dict[str, Any]] = []
    pin_tables: List[Dict[str, Any]] = []

    for page in pages_raw:
        page_number = int(page.get("page_number", 0))
        page_height = float(page.get("height", 0.0))
        text_blocks: List[Dict[str, Any]] = []

        for block in page.get("text_blocks_raw", []):
            block_type = _classify_block(
                block,
                page_height=page_height,
                median_font_size=median_font_size,
                top_counts=top_counts,
                bottom_counts=bottom_counts,
                total_pages=total_pages,
            )
            confidence = block.get("confidence", "deterministic")
            text_entry = {
                "text": str(block.get("text", "")).strip(),
                "bbox": block.get("bbox", [0, 0, 0, 0]),
                "block_type": block_type,
                "confidence": confidence,
                "font_size": float(block.get("font_size_max", 0.0)),
                "is_bold": bool(block.get("is_bold", False)),
            }
            text_blocks.append(text_entry)

            if block_type == "heading":
                for section_name, pattern in SECTION_PATTERNS:
                    if pattern.search(text_entry["text"]):
                        detected_sections.append(
                            {
                                "name": section_name,
                                "title": text_entry["text"],
                                "page": page_number,
                                "content_blocks": 0,
                            }
                        )
                        break

        page_tables = list(table_data.get("pages", {}).get(page_number, []))
        for table in page_tables:
            header_map = _normalize_header_names(table.get("columns", []))
            if {"pin", "description"}.issubset(set(header_map.values())):
                pin_tables.append(_build_pin_table_payload(table))

        normalized_pages.append(
            {
                "page_number": page_number,
                "text_blocks": text_blocks,
                "tables": page_tables,
                "figures": page.get("figures", []),
                "links": page.get("links", []),
                "warnings": page.get("warnings", []),
            }
        )

    for section in detected_sections:
        page_data = next((p for p in normalized_pages if p["page_number"] == section["page"]), None)
        if not page_data:
            continue
        section["content_blocks"] = sum(
            1 for block in page_data["text_blocks"] if block["block_type"] == "paragraph"
        )

    metadata = dict(raw.get("metadata", {}))
    metadata["extraction_method"] = raw.get("extraction_method", "unknown")
    metadata["ocr_pages"] = raw.get("ocr_pages", [])

    return {
        "source_pdf": raw.get("source_pdf", ""),
        "metadata": metadata,
        "pages": normalized_pages,
        "detected_sections": detected_sections,
        "pin_tables": pin_tables,
    }
