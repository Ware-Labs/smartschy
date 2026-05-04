#!/usr/bin/env python3
"""Deterministic table extraction for datasheet PDFs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ")
    return " ".join(text.split())


def _normalize_rows(rows: List[List[Any]]) -> List[List[str]]:
    normalized = [[_clean_cell(cell) for cell in row] for row in rows if row]
    if not normalized:
        return []
    width = max(len(row) for row in normalized)
    return [row + [""] * (width - len(row)) for row in normalized]


def _is_complex_table(rows: List[List[str]]) -> bool:
    if not rows:
        return True
    width = len(rows[0])
    if width < 2:
        return True
    empty_cells = sum(1 for row in rows for cell in row if not cell)
    total_cells = max(1, len(rows) * width)
    ragged = any(len(row) != width for row in rows)
    return ragged or (empty_cells / float(total_cells) > 0.6)


def _extract_with_pdfplumber(pdf_path: Path) -> Dict[str, Any]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("pdfplumber is required for table extraction") from exc

    pages: Dict[int, List[Dict[str, Any]]] = {}
    warnings: List[str] = []
    table_counter = 0
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            page_tables: List[Dict[str, Any]] = []
            try:
                candidates = page.find_tables()
            except Exception as exc:  # pragma: no cover - dependency/runtime variance
                warnings.append(f"page {page_index}: table detection failed: {exc}")
                candidates = []

            for table_idx, table in enumerate(candidates, start=1):
                try:
                    rows_raw = table.extract() or []
                except Exception as exc:  # pragma: no cover - dependency/runtime variance
                    warnings.append(f"page {page_index}: table {table_idx} extraction failed: {exc}")
                    continue
                rows = _normalize_rows(rows_raw)
                if not rows:
                    continue

                table_counter += 1
                headers = rows[0] if rows else []
                is_complex = _is_complex_table(rows)
                table_record = {
                    "table_id": f"tbl_{table_counter:04d}",
                    "page_number": page_index,
                    "bbox": [round(float(v), 3) for v in table.bbox] if table.bbox else [],
                    "columns": headers,
                    "rows": rows[1:] if len(rows) > 1 else [],
                    "raw_rows": rows,
                    "is_complex": is_complex,
                    "engine": "pdfplumber",
                    "notes": [],
                }
                page_tables.append(table_record)
            pages[page_index] = page_tables

    return {"engine": "pdfplumber", "pages": pages, "warnings": warnings}


def extract_tables(pdf_path: Path) -> Dict[str, Any]:
    """Extract deterministic tables and return per-page table payloads."""
    pdf_path = pdf_path.resolve()
    try:
        return _extract_with_pdfplumber(pdf_path)
    except RuntimeError as exc:
        return {
            "engine": "unavailable",
            "pages": {},
            "warnings": [str(exc)],
        }
