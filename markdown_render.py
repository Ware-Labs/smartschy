#!/usr/bin/env python3
"""Markdown renderer for normalized datasheet documents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


def _to_markdown_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    sep = ["---"] * width
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_table(table: Dict[str, Any]) -> str:
    rows = list(table.get("raw_rows", []))
    if not rows:
        columns = list(table.get("columns", []))
        body_rows = list(table.get("rows", []))
        rows = [columns] + body_rows if columns else body_rows

    block_lines: List[str] = []
    block_lines.append(f"#### Table ({table.get('table_id', 'unknown')})")
    if table.get("is_complex", False):
        block_lines.append("_Complex table preserved as structured JSON._")
        fallback_rows = rows if rows else [list(table.get("columns", []))] + list(table.get("rows", []))
        if fallback_rows:
            block_lines.append(_to_markdown_table(fallback_rows))
        block_lines.append("```json")
        block_lines.append(json.dumps(table, ensure_ascii=True, indent=2, sort_keys=True))
        block_lines.append("```")
    else:
        block_lines.append(_to_markdown_table(rows))

    notes = table.get("notes") or []
    if notes:
        block_lines.append("Notes:")
        for note in notes:
            block_lines.append(f"- {note}")
    return "\n".join(line for line in block_lines if line is not None).strip()


def _detect_title(normalized_doc: Dict[str, Any]) -> str:
    metadata = normalized_doc.get("metadata", {})
    title = str(metadata.get("title", "")).strip()
    if title:
        return title

    for page in normalized_doc.get("pages", []):
        for block in page.get("text_blocks", []):
            if block.get("block_type") == "heading":
                text = str(block.get("text", "")).strip()
                if text:
                    return text

    source_pdf = Path(str(normalized_doc.get("source_pdf", "datasheet"))).stem
    return source_pdf


def _section_outline(normalized_doc: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    seen = set()
    for section in normalized_doc.get("detected_sections", []):
        key = (section.get("name"), section.get("page"), section.get("title"))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {section.get('title', section.get('name', 'unknown'))} (page {section.get('page', '?')})")
    if not lines:
        lines.append("- No deterministic sections detected.")
    return lines


def render_document_markdown(normalized_doc: Dict[str, Any], output_dir: Path) -> str:
    """Render normalized JSON to a deterministic markdown document."""
    metadata = normalized_doc.get("metadata", {})
    ocr_pages = metadata.get("ocr_pages", [])
    warnings = []
    for page in normalized_doc.get("pages", []):
        for warning in page.get("warnings", []):
            warnings.append(f"page {page.get('page_number')}: {warning}")

    lines: List[str] = []
    lines.append(f"# Datasheet: {_detect_title(normalized_doc)}")
    lines.append("")
    lines.append("## Source Metadata")
    lines.append(f"- Source PDF: `{normalized_doc.get('source_pdf', '')}`")
    lines.append(f"- Page count: {metadata.get('page_count', len(normalized_doc.get('pages', [])))}")
    lines.append(f"- Extracted text method: {metadata.get('extraction_method', 'unknown')}")
    lines.append(f"- OCR pages: {', '.join(str(p) for p in ocr_pages) if ocr_pages else 'none'}")
    lines.append(f"- Extraction warnings: {len(warnings)}")
    lines.append("")

    lines.append("## Document Outline")
    lines.extend(_section_outline(normalized_doc))
    lines.append("")

    lines.append("## Sections")
    lines.append("")
    for page in normalized_doc.get("pages", []):
        page_number = int(page.get("page_number", 0))
        lines.append(f"<!-- source_page: {page_number} -->")
        lines.append(f"### Page {page_number}")
        lines.append("")

        for block in page.get("text_blocks", []):
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            block_type = block.get("block_type", "unknown")
            if block_type in {"header", "footer"}:
                continue
            if block_type == "heading":
                level = 4 if not re.match(r"^#+\s", text) else 5
                lines.append(f"{'#' * level} {text}")
                lines.append("")
            elif block_type == "caption":
                lines.append(f"*{text}*")
                lines.append("")
            else:
                lines.append(text)
                lines.append("")

        for table in page.get("tables", []):
            lines.append(_render_table(table))
            lines.append("")

        for figure in page.get("figures", []):
            image_path = figure.get("path", "")
            caption = figure.get("caption") or f"Figure from page {page_number}"
            lines.append(f"![{caption}]({image_path})")
            lines.append("")

        links = page.get("links", [])
        if links:
            lines.append("Links:")
            for link in links:
                target = link.get("target", "")
                lines.append(f"- {target}")
            lines.append("")

    if warnings:
        lines.append("## Extraction Warnings")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    rendered = "\n".join(lines).rstrip() + "\n"
    _ = output_dir  # kept for future path-aware rendering extensions
    return rendered
