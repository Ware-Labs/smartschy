"""PDF extraction using PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz
from datasheet_rag.parallel_ingest import (
    WorkerSelection,
    extract_tables_parallel,
    select_worker_count,
)
from datasheet_rag.table_extraction import (
    TableExtractionResult,
    TextLine,
    extract_tables_from_pages,
)

PARSER_NAME = "pymupdf"


@dataclass(slots=True)
class ExtractedPage:
    """Canonical per-page extraction result."""

    page_number: int
    text: str


@dataclass(slots=True)
class ExtractedDocument:
    """Canonical document-level extraction result."""

    metadata: dict[str, str]
    pages: list[ExtractedPage]


@dataclass(slots=True)
class ParsedDocumentBundle:
    """Single-pass extraction result for one PDF."""

    document: ExtractedDocument
    tables: TableExtractionResult
    worker_selection: WorkerSelection


def extract_pdf(pdf_path: Path) -> ExtractedDocument:
    """Extract document metadata and page text from a PDF."""

    return parse_pdf_bundle(pdf_path).document


def parse_pdf_bundle(
    pdf_path: Path,
    *,
    crop_dir: Path | None = None,
    workers: int | None = None,
) -> ParsedDocumentBundle:
    """Extract page text and tables from a PDF in a single document pass."""

    with fitz.open(pdf_path) as document:
        metadata = {
            key: value
            for key, value in document.metadata.items()
            if isinstance(value, str) and value.strip()
        }
        pages: list[ExtractedPage] = []
        page_texts: dict[int, str] = {}
        page_lines: dict[int, list[TextLine]] = {}
        for index, page in enumerate(document):
            page_number = index + 1
            page_text = page.get_text("text")
            page_texts[page_number] = page_text
            page_lines[page_number] = _extract_page_lines(page)
            pages.append(
                ExtractedPage(
                    page_number=page_number,
                    text=page_text,
                )
            )
        worker_selection = select_worker_count(
            pdf_path=pdf_path,
            page_count=document.page_count,
            page_lines=page_lines,
            manual_workers=workers,
        )
        if worker_selection.selected_worker_count == 1:
            table_result = extract_tables_from_pages(
                document=document,
                page_numbers=list(range(1, document.page_count + 1)),
                page_lines=page_lines,
                crop_dir=crop_dir,
            )
        else:
            table_result = extract_tables_parallel(
                pdf_path=pdf_path,
                page_numbers=list(range(1, document.page_count + 1)),
                page_lines=page_lines,
                worker_count=worker_selection.selected_worker_count,
                crop_dir=crop_dir,
            )

    return ParsedDocumentBundle(
        document=ExtractedDocument(metadata=metadata, pages=pages),
        tables=table_result,
        worker_selection=worker_selection,
    )


def _extract_page_lines(page: fitz.Page) -> list[TextLine]:
    """Build serializable per-line metadata for one page."""

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
