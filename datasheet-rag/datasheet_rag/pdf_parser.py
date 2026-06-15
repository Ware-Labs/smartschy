"""PDF extraction using PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz
from datasheet_rag.table_extraction import TableExtractionResult, extract_tables_from_document

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


def extract_pdf(pdf_path: Path) -> ExtractedDocument:
    """Extract document metadata and page text from a PDF."""

    return parse_pdf_bundle(pdf_path).document


def parse_pdf_bundle(
    pdf_path: Path,
    *,
    crop_dir: Path | None = None,
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
        for index, page in enumerate(document):
            page_number = index + 1
            page_text = page.get_text("text")
            page_texts[page_number] = page_text
            pages.append(
                ExtractedPage(
                    page_number=page_number,
                    text=page_text,
                )
            )
        table_result = extract_tables_from_document(document, page_texts, crop_dir=crop_dir)

    return ParsedDocumentBundle(
        document=ExtractedDocument(metadata=metadata, pages=pages),
        tables=table_result,
    )
