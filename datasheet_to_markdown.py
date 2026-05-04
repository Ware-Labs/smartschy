#!/usr/bin/env python3
"""Deterministic-first datasheet PDF processing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from llm_cleanup import cleanup_markdown_with_llm
from markdown_render import render_document_markdown
from normalize import normalize_document
from pdf_extract import extract_pdf
from table_extract import extract_tables


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _attach_tables_to_raw(raw: Dict[str, Any], table_data: Dict[str, Any]) -> Dict[str, Any]:
    by_page = table_data.get("pages", {})
    for page in raw.get("pages", []):
        page_number = int(page.get("page_number", 0))
        page["tables"] = by_page.get(page_number, [])
    raw.setdefault("warnings", [])
    raw["warnings"].extend(table_data.get("warnings", []))
    raw["table_engine"] = table_data.get("engine", "unknown")
    return raw


def build_extraction_report(
    raw: Dict[str, Any],
    normalized: Dict[str, Any],
    table_data: Dict[str, Any],
    *,
    ocr_char_threshold: int = 40,
) -> Dict[str, Any]:
    pages_raw = raw.get("pages", [])
    chars_per_page = {
        str(page.get("page_number", 0)): int(page.get("char_count", 0))
        for page in pages_raw
    }
    low_text_pages = [
        int(page.get("page_number", 0))
        for page in pages_raw
        if int(page.get("char_count", 0)) < int(ocr_char_threshold)
    ]
    suspected_ocr_pages = list(raw.get("ocr_pages", []))

    malformed_tables: List[Dict[str, Any]] = []
    for page_number, tables in table_data.get("pages", {}).items():
        for table in tables:
            if table.get("is_complex", False):
                malformed_tables.append(
                    {
                        "page": page_number,
                        "table_id": table.get("table_id", ""),
                        "reason": "table marked complex",
                    }
                )

    empty_sections = [
        {
            "name": section.get("name", ""),
            "title": section.get("title", ""),
            "page": section.get("page", 0),
        }
        for section in normalized.get("detected_sections", [])
        if int(section.get("content_blocks", 0)) == 0
    ]

    expected_page_count = int(raw.get("metadata", {}).get("page_count", 0))
    extracted_page_count = len(pages_raw)
    extraction_log: List[str] = []
    extraction_log.extend(raw.get("warnings", []))
    extraction_log.extend(table_data.get("warnings", []))
    for page in pages_raw:
        for warning in page.get("warnings", []):
            extraction_log.append(f"page {page.get('page_number', 0)}: {warning}")

    return {
        "source_pdf": raw.get("source_pdf", ""),
        "checks": {
            "page_count_matches": expected_page_count == extracted_page_count,
            "expected_page_count": expected_page_count,
            "extracted_page_count": extracted_page_count,
        },
        "chars_per_page": chars_per_page,
        "low_text_pages": low_text_pages,
        "suspected_ocr_pages": suspected_ocr_pages,
        "malformed_tables": malformed_tables,
        "empty_sections": empty_sections,
        "table_engine": table_data.get("engine", "unknown"),
        "ocr_char_threshold": int(ocr_char_threshold),
        "extraction_log": extraction_log,
    }


def process_datasheet_pdf(
    input_pdf: Path,
    out_dir: Path,
    *,
    use_llm: bool = False,
    mock_llm: bool = False,
    model: str = "gpt-4.1-mini",
    ocr: bool = False,
    ocr_char_threshold: int = 40,
    verbose: bool = False,
) -> Dict[str, Any]:
    input_pdf = Path(input_pdf).resolve()
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")

    out_dir = Path(out_dir).resolve()
    images_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[1/6] Extracting PDF content from: {input_pdf}")
    raw = extract_pdf(
        input_pdf,
        images_dir,
        use_ocr=ocr,
        ocr_char_threshold=ocr_char_threshold,
    )

    if verbose:
        print("[2/6] Extracting tables")
    table_data = extract_tables(input_pdf)
    raw = _attach_tables_to_raw(raw, table_data)
    _write_json(out_dir / "raw_extraction.json", raw)

    if verbose:
        print("[3/6] Normalizing extracted document")
    normalized = normalize_document(raw, table_data)
    _write_json(out_dir / "normalized_document.json", normalized)

    if verbose:
        print("[4/6] Rendering markdown")
    markdown = render_document_markdown(normalized, out_dir)

    cleanup_meta: Dict[str, Any] = {"cleanup_mode": "disabled", "llm_used": False, "warnings": []}
    if use_llm:
        if verbose:
            print("[5/6] Running optional LLM cleanup")
        markdown, cleanup_meta = cleanup_markdown_with_llm(
            markdown,
            normalized_document=normalized,
            model=model,
            mock=mock_llm,
        )

    datasheet_md = out_dir / "datasheet.md"
    datasheet_md.write_text(markdown, encoding="utf-8")

    if verbose:
        print("[6/6] Building extraction report")
    report = build_extraction_report(
        raw,
        normalized,
        table_data,
        ocr_char_threshold=ocr_char_threshold,
    )
    report["llm_cleanup"] = cleanup_meta
    _write_json(out_dir / "extraction_report.json", report)

    print(f"Wrote: {out_dir / 'raw_extraction.json'}")
    print(f"Wrote: {out_dir / 'normalized_document.json'}")
    print(f"Wrote: {out_dir / 'extraction_report.json'}")
    print(f"Wrote: {datasheet_md}")
    print(f"Images: {images_dir}")
    return {
        "input_pdf": str(input_pdf),
        "output_dir": str(out_dir),
        "raw_extraction": str(out_dir / "raw_extraction.json"),
        "normalized_document": str(out_dir / "normalized_document.json"),
        "extraction_report": str(out_dir / "extraction_report.json"),
        "datasheet_markdown": str(datasheet_md),
        "images_dir": str(images_dir),
        "raw": raw,
        "table_data": table_data,
        "normalized": normalized,
        "report": report,
        "cleanup_meta": cleanup_meta,
    }
