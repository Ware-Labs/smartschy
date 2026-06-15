"""Storage and retrieval services for Phase 3."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from datasheet_rag import __version__
from datasheet_rag.chunking import ChunkRecord, chunk_pages
from datasheet_rag.database import connect, initialize
from datasheet_rag.pdf_parser import PARSER_NAME, ExtractedDocument, parse_pdf_bundle
from datasheet_rag.table_extraction import (
    ExtractedTable,
    ExtractedTableRow,
    TABLE_PARSER_VERSION,
    VISUAL_TABLE_PARSER,
)


@dataclass(slots=True)
class PageRecord:
    """Stored representation of a page."""

    page_number: int
    text: str
    text_length: int


@dataclass(slots=True)
class DocumentRecord:
    """Stored representation of a document."""

    document_id: str
    file_hash: str
    source_path: str
    file_name: str
    file_size_bytes: int
    parser_name: str
    page_count: int
    metadata: dict[str, str]
    created_at: str
    updated_at: str
    pages: list[PageRecord]
    chunk_count: int
    table_count: int
    table_row_count: int
    table_candidate_count: int


@dataclass(slots=True)
class StoredTableRecord:
    """Stored representation of a detected table."""

    page_number: int
    table_index: int
    table_title: str | None
    section_title: str | None
    headers: list[str | None]
    row_count: int
    column_count: int
    bbox: list[float]
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
class SearchResult:
    """Search result returned from lexical chunk retrieval."""

    document_id: str
    page_number: int
    chunk_index: int
    chunk_type: str
    source_text: str
    bm25_score: float
    score: float
    table_index: int | None = None
    row_index: int | None = None
    table_title: str | None = None
    section_title: str | None = None
    headers: list[str | None] | None = None
    cells: list[str | None] | None = None
    row_type: str | None = None
    crop_path: str | None = None


@dataclass(slots=True)
class IngestResult:
    """Return value for ingest operations."""

    document_id: str
    db_path: Path
    output_dir: Path
    page_count: int
    chunk_count: int
    table_count: int
    table_row_count: int
    table_candidate_count: int
    replaced_existing: bool
    skipped_existing: bool


def build_document_id(pdf_path: Path) -> tuple[str, str]:
    """Build a stable document identifier from the file name and content hash."""

    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    file_hash = digest.hexdigest()
    stem = "".join(
        character.lower() if character.isalnum() else "-"
        for character in pdf_path.stem
    ).strip("-")
    slug = "-".join(part for part in stem.split("-") if part) or "document"
    return f"{slug}-{file_hash[:12]}", file_hash


def ingest_pdf(
    pdf_path: Path,
    db_path: Path,
    output_dir: Path,
    *,
    force: bool = False,
) -> IngestResult:
    """Extract a PDF, store it in SQLite, and emit debug artifacts."""

    pdf_path = pdf_path.resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    generated_document_id, file_hash = build_document_id(pdf_path)

    with connect(db_path) as connection:
        initialize(connection)
        existing = connection.execute(
            """
            SELECT
                document_id,
                created_at,
                page_count,
                (
                    SELECT COUNT(*)
                    FROM chunks
                    WHERE chunks.document_id = documents.document_id
                ) AS chunk_count,
                (
                    SELECT COUNT(*)
                    FROM tables
                    WHERE tables.document_id = documents.document_id
                ) AS table_count,
                (
                    SELECT COUNT(*)
                    FROM table_rows
                    WHERE table_rows.document_id = documents.document_id
                ) AS table_row_count,
                COALESCE(
                    JSON_EXTRACT(metadata_json, '$.table_candidate_count'),
                    0
                ) AS table_candidate_count,
                JSON_EXTRACT(metadata_json, '$.table_parser_version') AS table_parser_version
            FROM documents
            WHERE file_hash = ? OR document_id = ?
            ORDER BY CASE WHEN file_hash = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (file_hash, generated_document_id, file_hash),
        ).fetchone()
        document_id = existing["document_id"] if existing else generated_document_id
        artifact_dir = output_dir.resolve() / document_id

        if (
            existing is not None
            and not force
            and existing["table_parser_version"] == TABLE_PARSER_VERSION
            and _artifacts_exist(artifact_dir)
        ):
            return IngestResult(
                document_id=document_id,
                db_path=db_path.resolve(),
                output_dir=artifact_dir,
                page_count=existing["page_count"],
                chunk_count=existing["chunk_count"],
                table_count=existing["table_count"],
                table_row_count=existing["table_row_count"],
                table_candidate_count=existing["table_candidate_count"],
                replaced_existing=False,
                skipped_existing=True,
            )

        artifact_dir.mkdir(parents=True, exist_ok=True)
        parsed_bundle = parse_pdf_bundle(pdf_path, crop_dir=artifact_dir / "table_crops")
        extracted = parsed_bundle.document
        extracted_tables = parsed_bundle.tables
        chunks = chunk_pages(
            [(page.page_number, page.text) for page in extracted.pages]
        )
        now = datetime.now(UTC).isoformat()
        file_size = pdf_path.stat().st_size
        created_at = existing["created_at"] if existing else now

        connection.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM tables WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM table_rows WHERE document_id = ?", (document_id,))
        connection.execute(
            """
            INSERT INTO documents (
                document_id,
                file_hash,
                source_path,
                file_name,
                file_size_bytes,
                parser_name,
                page_count,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                file_hash = excluded.file_hash,
                source_path = excluded.source_path,
                file_name = excluded.file_name,
                file_size_bytes = excluded.file_size_bytes,
                parser_name = excluded.parser_name,
                page_count = excluded.page_count,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                document_id,
                file_hash,
                str(pdf_path),
                pdf_path.name,
                file_size,
                PARSER_NAME,
                len(extracted.pages),
                json.dumps(
                    {
                        **extracted.metadata,
                        "table_candidate_count": extracted_tables.candidate_count,
                        "table_parser_version": TABLE_PARSER_VERSION,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                created_at,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO pages (document_id, page_number, page_text, text_length)
            VALUES (?, ?, ?, ?)
            """,
            [
                (document_id, page.page_number, page.text, len(page.text))
                for page in extracted.pages
            ],
        )
        connection.executemany(
            """
            INSERT INTO chunks (
                document_id,
                page_number,
                chunk_index,
                chunk_type,
                source_text,
                text_length
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    chunk.page_number,
                    chunk.chunk_index,
                    chunk.chunk_type,
                    chunk.source_text,
                    len(chunk.source_text),
                )
                for chunk in chunks
            ],
        )
        connection.executemany(
            """
            INSERT INTO tables (
                document_id,
                page_number,
                table_index,
                table_title,
                section_title,
                headers_json,
                row_count,
                column_count,
                bbox_json,
                detection_source,
                crop_path,
                visual_parser,
                native_bbox_text,
                confidence_json,
                parser_family,
                parser_mode,
                table_kind,
                header_signature,
                region_sources_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    table.page_number,
                    table.table_index,
                    table.table_title,
                    table.section_title,
                    json.dumps(table.headers, ensure_ascii=False),
                    table.row_count,
                    table.column_count,
                    json.dumps(list(table.bbox)),
                    table.detection_source,
                    table.crop_path,
                    table.visual_parser,
                    table.native_bbox_text,
                    json.dumps(table.confidence_summary, ensure_ascii=False, sort_keys=True),
                    table.parser_family,
                    table.parser_mode,
                    table.table_kind,
                    table.header_signature,
                    json.dumps(table.region_sources, ensure_ascii=False),
                )
                for table in extracted_tables.tables
            ],
        )
        connection.executemany(
            """
            INSERT INTO table_rows (
                document_id,
                page_number,
                table_index,
                row_index,
                chunk_type,
                table_title,
                section_title,
                headers_json,
                cells_json,
                text_rendering,
                text_length,
                row_type,
                visual_cells_json,
                native_fallback_text,
                native_fallback_cells_json,
                confidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    row.page_number,
                    row.table_index,
                    row.row_index,
                    row.chunk_type,
                    row.table_title,
                    row.section_title,
                    json.dumps(row.headers, ensure_ascii=False),
                    json.dumps(row.cells, ensure_ascii=False),
                    row.text_rendering,
                    len(row.text_rendering),
                    row.row_type,
                    json.dumps(row.visual_cells, ensure_ascii=False),
                    row.native_fallback_text,
                    json.dumps(row.native_fallback_cells, ensure_ascii=False),
                    json.dumps(row.confidence_summary, ensure_ascii=False, sort_keys=True),
                )
                for row in extracted_tables.rows
            ],
        )
        connection.commit()

    _write_debug_artifacts(
        artifact_dir=artifact_dir,
        document_id=document_id,
        pdf_path=pdf_path,
        file_hash=file_hash,
        extracted=extracted,
        chunks=chunks,
        tables=extracted_tables.tables,
        table_rows=extracted_tables.rows,
        table_candidate_count=extracted_tables.candidate_count,
    )

    return IngestResult(
        document_id=document_id,
        db_path=db_path.resolve(),
        output_dir=artifact_dir,
        page_count=len(extracted.pages),
        chunk_count=len(chunks),
        table_count=len(extracted_tables.tables),
        table_row_count=len(extracted_tables.rows),
        table_candidate_count=extracted_tables.candidate_count,
        replaced_existing=existing is not None,
        skipped_existing=False,
    )


def _artifacts_exist(artifact_dir: Path) -> bool:
    """Return whether the expected debug artifacts already exist for a document."""

    required_files = (
        "pages.jsonl",
        "chunks.jsonl",
        "tables.jsonl",
        "table_rows.jsonl",
        "document_summary.json",
    )
    return all((artifact_dir / file_name).exists() for file_name in required_files)


def inspect_documents(db_path: Path, document_id: str | None = None) -> list[DocumentRecord]:
    """Load stored document metadata and pages from SQLite."""

    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with connect(db_path) as connection:
        initialize(connection)
        if document_id is None:
            rows = connection.execute(
                """
                SELECT
                    document_id,
                    file_hash,
                    source_path,
                    file_name,
                    file_size_bytes,
                    parser_name,
                    page_count,
                    metadata_json,
                    created_at,
                    updated_at
                FROM documents
                ORDER BY updated_at DESC, document_id ASC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    document_id,
                    file_hash,
                    source_path,
                    file_name,
                    file_size_bytes,
                    parser_name,
                    page_count,
                    metadata_json,
                    created_at,
                    updated_at
                FROM documents
                WHERE document_id = ?
                """,
                (document_id,),
            ).fetchall()

        documents: list[DocumentRecord] = []
        for row in rows:
            page_rows = connection.execute(
                """
                SELECT page_number, page_text, text_length
                FROM pages
                WHERE document_id = ?
                ORDER BY page_number ASC
                """,
                (row["document_id"],),
            ).fetchall()
            chunk_count_row = connection.execute(
                """
                SELECT COUNT(*) AS chunk_count
                FROM chunks
                WHERE document_id = ?
                """,
                (row["document_id"],),
            ).fetchone()
            table_count_row = connection.execute(
                """
                SELECT COUNT(*) AS table_count
                FROM tables
                WHERE document_id = ?
                """,
                (row["document_id"],),
            ).fetchone()
            table_row_count_row = connection.execute(
                """
                SELECT COUNT(*) AS table_row_count
                FROM table_rows
                WHERE document_id = ?
                """,
                (row["document_id"],),
            ).fetchone()
            candidate_count = json.loads(row["metadata_json"]).get("table_candidate_count", 0)
            documents.append(
                DocumentRecord(
                    document_id=row["document_id"],
                    file_hash=row["file_hash"],
                    source_path=row["source_path"],
                    file_name=row["file_name"],
                    file_size_bytes=row["file_size_bytes"],
                    parser_name=row["parser_name"],
                    page_count=row["page_count"],
                    metadata=json.loads(row["metadata_json"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    pages=[
                        PageRecord(
                            page_number=page_row["page_number"],
                            text=page_row["page_text"],
                            text_length=page_row["text_length"],
                        )
                        for page_row in page_rows
                    ],
                    chunk_count=chunk_count_row["chunk_count"],
                    table_count=table_count_row["table_count"],
                    table_row_count=table_row_count_row["table_row_count"],
                    table_candidate_count=candidate_count,
                )
            )

    if document_id is not None and not documents:
        raise LookupError(f"Document not found: {document_id}")

    return documents


def _write_debug_artifacts(
    *,
    artifact_dir: Path,
    document_id: str,
    pdf_path: Path,
    file_hash: str,
    extracted: ExtractedDocument,
    chunks: list[ChunkRecord],
    tables: list[ExtractedTable],
    table_rows: list[ExtractedTableRow],
    table_candidate_count: int,
) -> None:
    """Write inspectable Phase 3 debug outputs."""

    pages_path = artifact_dir / "pages.jsonl"
    with pages_path.open("w", encoding="utf-8") as handle:
        for page in extracted.pages:
            json.dump(
                {
                    "document_id": document_id,
                    "page_number": page.page_number,
                    "text": page.text,
                    "text_length": len(page.text),
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    chunks_path = artifact_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            json.dump(
                {
                    "document_id": document_id,
                    "page_number": chunk.page_number,
                    "chunk_index": chunk.chunk_index,
                    "chunk_type": chunk.chunk_type,
                    "source_text": chunk.source_text,
                    "text_length": len(chunk.source_text),
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    tables_path = artifact_dir / "tables.jsonl"
    with tables_path.open("w", encoding="utf-8") as handle:
        for table in tables:
            json.dump(
                {
                    "document_id": document_id,
                    "page_number": table.page_number,
                    "table_index": table.table_index,
                    "table_title": table.table_title,
                    "section_title": table.section_title,
                    "headers": table.headers,
                    "row_count": table.row_count,
                    "column_count": table.column_count,
                    "bbox": list(table.bbox),
                    "detection_source": table.detection_source,
                    "crop_path": table.crop_path,
                    "visual_parser": table.visual_parser,
                    "native_bbox_text": table.native_bbox_text,
                    "confidence_summary": table.confidence_summary,
                    "parser_family": table.parser_family,
                    "parser_mode": table.parser_mode,
                    "table_kind": table.table_kind,
                    "header_signature": table.header_signature,
                    "region_sources": table.region_sources,
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    table_rows_path = artifact_dir / "table_rows.jsonl"
    with table_rows_path.open("w", encoding="utf-8") as handle:
        for row in table_rows:
            json.dump(
                {
                    "document_id": document_id,
                    "page_number": row.page_number,
                    "table_index": row.table_index,
                    "row_index": row.row_index,
                    "chunk_type": row.chunk_type,
                    "row_type": row.row_type,
                    "table_title": row.table_title,
                    "section_title": row.section_title,
                    "headers": row.headers,
                    "cells": row.cells,
                    "visual_cells": row.visual_cells,
                    "native_fallback_text": row.native_fallback_text,
                    "native_fallback_cells": row.native_fallback_cells,
                    "confidence_summary": row.confidence_summary,
                    "text_rendering": row.text_rendering,
                    "text_length": len(row.text_rendering),
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    summary = {
        "document_id": document_id,
        "source_path": str(pdf_path),
        "file_name": pdf_path.name,
        "file_hash": file_hash,
        "page_count": len(extracted.pages),
        "chunk_count": len(chunks),
        "table_count": len(tables),
        "table_row_count": len(table_rows),
        "table_candidate_count": table_candidate_count,
        "parser_name": PARSER_NAME,
        "metadata": extracted.metadata,
        "artifacts": {
            "pages_jsonl": str(pages_path),
            "chunks_jsonl": str(chunks_path),
            "tables_jsonl": str(tables_path),
            "table_rows_jsonl": str(table_rows_path),
            "table_crops_dir": str((artifact_dir / "table_crops").resolve()),
        },
        "generated_by": {
            "package_version": __version__,
        },
    }
    (artifact_dir / "document_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def format_document_summary(document: DocumentRecord) -> str:
    """Render a concise human-readable inspection summary."""

    lines = [
        f"document_id: {document.document_id}",
        f"file_name: {document.file_name}",
        f"source_path: {document.source_path}",
        f"file_hash: {document.file_hash}",
        f"page_count: {document.page_count}",
        f"chunk_count: {document.chunk_count}",
        f"table_count: {document.table_count}",
        f"table_row_count: {document.table_row_count}",
        f"table_candidate_count: {document.table_candidate_count}",
        f"parser_name: {document.parser_name}",
        f"created_at: {document.created_at}",
        f"updated_at: {document.updated_at}",
    ]

    if document.metadata:
        lines.append("metadata:")
        for key in sorted(document.metadata):
            lines.append(f"  {key}: {document.metadata[key]}")

    lines.append("pages:")
    for page in document.pages:
        preview = " ".join(page.text.split())
        if len(preview) > 80:
            preview = f"{preview[:77]}..."
        lines.append(
            f"  - page {page.page_number}: chars={page.text_length}; preview={preview or '<empty>'}"
        )

    return "\n".join(lines)


def serialize_document(document: DocumentRecord) -> dict[str, object]:
    """Expose a document record as JSON-compatible data."""

    payload = asdict(document)
    return payload


def search_records(db_path: Path, query_text: str, limit: int = 5) -> list[SearchResult]:
    """Run SQLite FTS5 search over both prose chunks and extracted table rows."""

    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    match_query = _build_match_query(query_text)
    if not match_query:
        return []
    query_tokens = _normalize_tokens(query_text)
    query_phrase = " ".join(query_tokens)

    with connect(db_path) as connection:
        initialize(connection)
        chunk_rows = connection.execute(
            """
            SELECT
                chunks.document_id,
                chunks.page_number,
                chunks.chunk_index,
                chunks.chunk_type,
                chunks.source_text,
                bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY score ASC, chunks.document_id ASC, chunks.page_number ASC, chunks.chunk_index ASC
            LIMIT ?
            """,
            (match_query, max(limit * 10, 50)),
        ).fetchall()
        table_rows = connection.execute(
            """
            SELECT
                table_rows.document_id,
                table_rows.page_number,
                table_rows.table_index,
                table_rows.row_index,
                table_rows.chunk_type,
                table_rows.table_title,
                table_rows.section_title,
                table_rows.headers_json,
                table_rows.cells_json,
                table_rows.row_type,
                tables.crop_path,
                table_rows.text_rendering,
                bm25(table_rows_fts) AS score
            FROM table_rows_fts
            JOIN table_rows ON table_rows.id = table_rows_fts.rowid
            LEFT JOIN tables
              ON tables.document_id = table_rows.document_id
             AND tables.page_number = table_rows.page_number
             AND tables.table_index = table_rows.table_index
            WHERE table_rows_fts MATCH ?
            ORDER BY score ASC, table_rows.document_id ASC, table_rows.page_number ASC, table_rows.row_index ASC
            LIMIT ?
            """,
            (match_query, max(limit * 10, 50)),
        ).fetchall()

    ranked_results = [
        SearchResult(
            document_id=row["document_id"],
            page_number=row["page_number"],
            chunk_index=row["chunk_index"],
            chunk_type=row["chunk_type"],
            source_text=row["source_text"],
            bm25_score=float(row["score"]),
            score=_rank_result(
                chunk_type=row["chunk_type"],
                source_text=row["source_text"],
                bm25_score=float(row["score"]),
                query_tokens=query_tokens,
                query_phrase=query_phrase,
                raw_query=query_text,
                headers=None,
                cells=None,
            ),
        )
        for row in chunk_rows
    ]
    ranked_results.extend(
        SearchResult(
            document_id=row["document_id"],
            page_number=row["page_number"],
            chunk_index=row["row_index"],
            chunk_type=row["chunk_type"],
            source_text=row["text_rendering"],
            bm25_score=float(row["score"]),
            score=_rank_result(
                chunk_type=row["chunk_type"],
                source_text=row["text_rendering"],
                bm25_score=float(row["score"]),
                query_tokens=query_tokens,
                query_phrase=query_phrase,
                raw_query=query_text,
                headers=json.loads(row["headers_json"]),
                cells=json.loads(row["cells_json"]),
                row_type=row["row_type"],
            ),
            table_index=row["table_index"],
            row_index=row["row_index"],
            table_title=row["table_title"],
            section_title=row["section_title"],
            headers=json.loads(row["headers_json"]),
            cells=json.loads(row["cells_json"]),
            row_type=row["row_type"],
            crop_path=row["crop_path"],
        )
        for row in table_rows
    )
    ranked_results.sort(
        key=lambda item: (
            -item.score,
            item.page_number,
            item.chunk_index,
            item.document_id,
        )
    )
    return ranked_results[:limit]


def _rank_result(
    *,
    chunk_type: str,
    source_text: str,
    bm25_score: float,
    query_tokens: list[str],
    query_phrase: str,
    raw_query: str,
    headers: list[str | None] | None,
    cells: list[str | None] | None,
    row_type: str | None = None,
) -> float:
    """Combine BM25 with lightweight lexical heuristics for better debug search results."""

    normalized_text = _normalize_text(source_text)
    raw_lower = source_text.lower()
    lexical_score = -bm25_score
    score = lexical_score * 8.0

    if not query_tokens:
        return score

    token_hits = sum(1 for token in query_tokens if token in normalized_text)
    score += (token_hits / len(query_tokens)) * 25.0

    phrase_position = normalized_text.find(query_phrase) if query_phrase else -1
    if phrase_position >= 0:
        score += 80.0
        if phrase_position < 120:
            score += 40.0
        if phrase_position < 40:
            score += 15.0

    raw_query = raw_query.strip().lower()
    if raw_query and raw_query in raw_lower:
        score += 110.0
        if chunk_type == "table_row":
            score += 60.0

    if _looks_like_section_heading(source_text, query_phrase):
        score += 25.0

    if _looks_like_table_of_contents(source_text):
        score -= 35.0

    if chunk_type == "table_row":
        score += 30.0
        score += _table_row_quality_bonus(
            raw_query=raw_query,
            headers=headers or [],
            cells=cells or [],
        )
        if row_type == "group_header":
            score -= 30.0

    return score


def _table_row_quality_bonus(
    *,
    raw_query: str,
    headers: list[str | None],
    cells: list[str | None],
) -> float:
    """Prefer richer structured rows over sparse figure-like table detections."""

    score = 0.0
    populated_cells = [cell for cell in cells if cell]
    score += min(len(populated_cells), 6) * 8.0

    normalized_headers = [
        "" if header is None else re.sub(r"\s+", " ", header).strip().lower()
        for header in headers
    ]
    if any(header in {"pin", "name", "function", "description"} for header in normalized_headers):
        score += 35.0

    query_lower = raw_query.strip().lower()
    for header, cell in zip(normalized_headers, cells):
        if not cell:
            continue
        cell_lower = cell.lower()
        if header in {"name", "pin"} and query_lower and query_lower in cell_lower:
            score += 60.0
        if header in {"function", "description"} and "/" in cell:
            score += 12.0

    if len(populated_cells) <= 2:
        score -= 40.0
    if not any(normalized_headers):
        score -= 20.0

    return score


def _looks_like_section_heading(source_text: str, query_phrase: str) -> bool:
    """Detect likely section-heading matches near the start of the chunk."""

    if not query_phrase:
        return False
    start_window = _normalize_text(source_text[:180])
    pattern = rf"(?:^| )\d+(?:\.\d+)+ {re.escape(query_phrase)}(?:\b| )"
    if re.search(pattern, start_window):
        return True
    return query_phrase in start_window


def _looks_like_table_of_contents(source_text: str) -> bool:
    """Detect table-of-contents style chunks that should rank below body content."""

    if re.search(r"\.\s*\.\s*\.\s*\.", source_text):
        return True
    if source_text.count(" .") > 10:
        return True
    return False


def _normalize_text(text: str) -> str:
    """Normalize text for lexical matching."""

    return " ".join(_normalize_tokens(text))


def _normalize_tokens(text: str) -> list[str]:
    """Extract lowercase alphanumeric tokens."""

    return [token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text)]


def _build_match_query(query_text: str) -> str:
    """Convert raw user input into a safe FTS5 query."""

    tokens = _normalize_tokens(query_text)
    if not tokens:
        return ""

    clauses: list[str] = []
    for token in tokens:
        variants = {token}
        if token.endswith("s") and len(token) > 4:
            variants.add(token[:-1])
        elif len(token) > 4:
            variants.add(f"{token}s")

        variant_terms = " OR ".join(f'"{variant}"' for variant in sorted(variants))
        clauses.append(f"({variant_terms})")
    return " AND ".join(clauses)
