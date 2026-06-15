"""Storage, retrieval, and grounded query services."""

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
from datasheet_rag.entities import (
    ENTITY_PARSER_VERSION,
    QueryResponse,
    evaluate_cases,
    extract_document_entities,
    load_eval_cases,
    normalize_entity_key,
)
from datasheet_rag.parallel_ingest import WorkerSelection, serialize_worker_selection
from datasheet_rag.pdf_parser import PARSER_NAME, ExtractedDocument, parse_pdf_bundle
from datasheet_rag.query_planner import QueryPlannerResult, QueryRetrievalSpec, plan_query
from datasheet_rag.query_reranker import QueryGroupRerankResult, rerank_query_groups
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
    entity_count: int
    entity_relation_count: int


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
    entity_family: str | None = None
    entity_display_text: str | None = None


@dataclass(slots=True)
class QueryGroupCandidate:
    """Candidate evidence group before final reranking and serialization."""

    group_id: str
    document_id: str
    page_number: int
    table_index: int | None
    section_title: str | None
    table_title: str | None
    evidence_family: str
    local_score: float
    quality_score: float
    summary: str
    items: list[SearchResult]
    sample_texts: list[str]


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
    worker_mode: str
    selected_worker_count: int
    probe_ran: bool
    entity_count: int
    entity_relation_count: int


QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "canonical",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "their",
    "there",
    "these",
    "this",
    "those",
    "to",
    "use",
    "what",
    "which",
    "with",
}
QUERY_FEATURE_TERMS = {
    "adc",
    "ain",
    "ble",
    "bluetooth",
    "can",
    "csn",
    "dac",
    "enable",
    "gpio",
    "i2c",
    "i3c",
    "input",
    "mode",
    "output",
    "pll",
    "pwm",
    "qspi",
    "radio",
    "sck",
    "shutdown",
    "spi",
    "swd",
    "swo",
    "trace",
    "twi",
    "uart",
    "uarte",
    "usb",
    "xosc",
}


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
    workers: int | None = None,
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
                metadata_json,
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

        existing_metadata = json.loads(existing["metadata_json"]) if existing else {}
        if (
            existing is not None
            and not force
            and existing["table_parser_version"] == TABLE_PARSER_VERSION
            and existing_metadata.get("entity_parser_version") == ENTITY_PARSER_VERSION
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
                worker_mode=existing_metadata.get("worker_mode", "auto"),
                selected_worker_count=existing_metadata.get("selected_worker_count", 1),
                probe_ran=bool(existing_metadata.get("probe_ran", False)),
                entity_count=existing_metadata.get("entity_count", 0),
                entity_relation_count=existing_metadata.get("entity_relation_count", 0),
            )

        artifact_dir.mkdir(parents=True, exist_ok=True)
        parsed_bundle = parse_pdf_bundle(
            pdf_path,
            crop_dir=artifact_dir / "table_crops",
            workers=workers,
        )
        extracted = parsed_bundle.document
        extracted_tables = parsed_bundle.tables
        worker_selection = parsed_bundle.worker_selection
        chunks = chunk_pages(
            [(page.page_number, page.text) for page in extracted.pages]
        )
        entities = extract_document_entities(
            document_id=document_id,
            source_path=pdf_path,
            metadata=extracted.metadata,
            chunks=chunks,
            tables=extracted_tables.tables,
            table_rows=extracted_tables.rows,
        )
        now = datetime.now(UTC).isoformat()
        file_size = pdf_path.stat().st_size
        created_at = existing["created_at"] if existing else now

        connection.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM tables WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM table_rows WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM entities WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM entity_evidence WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM entity_relations WHERE document_id = ?", (document_id,))
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
                        "entity_parser_version": ENTITY_PARSER_VERSION,
                        "entity_count": len(entities.entities),
                        "entity_relation_count": len(entities.relations),
                        **serialize_worker_selection(worker_selection),
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
        connection.executemany(
            """
            INSERT INTO entities (
                document_id,
                entity_key,
                entity_family,
                normalized_key,
                display_text,
                raw_text,
                aliases_json,
                confidence,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    entity.entity_key,
                    entity.entity_family,
                    entity.normalized_key,
                    entity.display_text,
                    entity.raw_text,
                    json.dumps(entity.aliases, ensure_ascii=False),
                    entity.confidence,
                    json.dumps(entity.metadata, ensure_ascii=False, sort_keys=True),
                )
                for entity in entities.entities
            ],
        )
        connection.executemany(
            """
            INSERT INTO entity_evidence (
                document_id,
                entity_key,
                page_number,
                source_kind,
                table_index,
                row_index,
                chunk_index,
                evidence_text,
                confidence,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    evidence.entity_key,
                    evidence.page_number,
                    evidence.source_kind,
                    evidence.table_index,
                    evidence.row_index,
                    evidence.chunk_index,
                    evidence.evidence_text,
                    evidence.confidence,
                    json.dumps(evidence.metadata or {}, ensure_ascii=False, sort_keys=True),
                )
                for evidence in entities.evidence
            ],
        )
        connection.executemany(
            """
            INSERT INTO entity_relations (
                document_id,
                source_entity_key,
                relation_type,
                target_entity_key,
                confidence,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    relation.source_entity_key,
                    relation.relation_type,
                    relation.target_entity_key,
                    relation.confidence,
                    json.dumps(relation.metadata, ensure_ascii=False, sort_keys=True),
                )
                for relation in entities.relations
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
        worker_selection=worker_selection,
        entity_result=entities,
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
        worker_mode=worker_selection.worker_mode,
        selected_worker_count=worker_selection.selected_worker_count,
        probe_ran=worker_selection.probe_ran,
        entity_count=len(entities.entities),
        entity_relation_count=len(entities.relations),
    )


def _artifacts_exist(artifact_dir: Path) -> bool:
    """Return whether the expected debug artifacts already exist for a document."""

    required_files = (
        "pages.jsonl",
        "chunks.jsonl",
        "tables.jsonl",
        "table_rows.jsonl",
        "entities.jsonl",
        "entity_evidence.jsonl",
        "entity_relations.jsonl",
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
            entity_count_row = connection.execute(
                """
                SELECT COUNT(*) AS entity_count
                FROM entities
                WHERE document_id = ?
                """,
                (row["document_id"],),
            ).fetchone()
            entity_relation_count_row = connection.execute(
                """
                SELECT COUNT(*) AS entity_relation_count
                FROM entity_relations
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
                    entity_count=entity_count_row["entity_count"],
                    entity_relation_count=entity_relation_count_row["entity_relation_count"],
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
    worker_selection: WorkerSelection,
    entity_result: object,
) -> None:
    """Write inspectable debug outputs."""

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

    entities_path = artifact_dir / "entities.jsonl"
    with entities_path.open("w", encoding="utf-8") as handle:
        for entity in entity_result.entities:
            json.dump(
                {
                    "document_id": document_id,
                    "entity_key": entity.entity_key,
                    "entity_family": entity.entity_family,
                    "normalized_key": entity.normalized_key,
                    "display_text": entity.display_text,
                    "raw_text": entity.raw_text,
                    "aliases": entity.aliases,
                    "confidence": entity.confidence,
                    "metadata": entity.metadata,
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    entity_evidence_path = artifact_dir / "entity_evidence.jsonl"
    with entity_evidence_path.open("w", encoding="utf-8") as handle:
        for evidence in entity_result.evidence:
            json.dump(
                {
                    "document_id": document_id,
                    "entity_key": evidence.entity_key,
                    "page_number": evidence.page_number,
                    "source_kind": evidence.source_kind,
                    "table_index": evidence.table_index,
                    "row_index": evidence.row_index,
                    "chunk_index": evidence.chunk_index,
                    "evidence_text": evidence.evidence_text,
                    "confidence": evidence.confidence,
                    "metadata": evidence.metadata or {},
                },
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

    entity_relations_path = artifact_dir / "entity_relations.jsonl"
    with entity_relations_path.open("w", encoding="utf-8") as handle:
        for relation in entity_result.relations:
            json.dump(
                {
                    "document_id": document_id,
                    "source_entity_key": relation.source_entity_key,
                    "relation_type": relation.relation_type,
                    "target_entity_key": relation.target_entity_key,
                    "confidence": relation.confidence,
                    "metadata": relation.metadata,
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
        "entity_count": len(entity_result.entities),
        "entity_relation_count": len(entity_result.relations),
        "parser_name": PARSER_NAME,
        "metadata": extracted.metadata,
        "worker_selection": serialize_worker_selection(worker_selection),
        "artifacts": {
            "pages_jsonl": str(pages_path),
            "chunks_jsonl": str(chunks_path),
            "tables_jsonl": str(tables_path),
            "table_rows_jsonl": str(table_rows_path),
            "entities_jsonl": str(entities_path),
            "entity_evidence_jsonl": str(entity_evidence_path),
            "entity_relations_jsonl": str(entity_relations_path),
            "table_crops_dir": str((artifact_dir / "table_crops").resolve()),
        },
        "generated_by": {
            "package_version": __version__,
            "entity_parser_version": ENTITY_PARSER_VERSION,
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
        f"entity_count: {document.entity_count}",
        f"entity_relation_count: {document.entity_relation_count}",
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


def _search_records_with_connection(
    *,
    connection: object,
    query_text: str,
    limit: int,
) -> list[SearchResult]:
    """Run the shared retrieval query against an already-open SQLite connection."""

    match_query = _build_match_query(query_text)
    query_tokens = _normalize_tokens(query_text)
    query_phrase = " ".join(query_tokens)
    normalized_query = normalize_entity_key(query_text)
    if not query_tokens:
        return []

    candidate_limit = max(limit * 10, 50)
    ranked_results: list[SearchResult] = []
    if match_query:
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
            (match_query, candidate_limit),
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
            (match_query, candidate_limit),
        ).fetchall()
        ranked_results.extend(
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
        )
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

    ranked_results.extend(
        _entity_match_results(
            connection=connection,
            query_text=query_text,
            query_tokens=query_tokens,
            normalized_query=normalized_query,
        )
    )
    deduped = _dedupe_results(ranked_results)
    deduped.sort(
        key=lambda item: (
            -item.score,
            item.page_number,
            item.chunk_index,
            item.document_id,
        )
    )
    return deduped[:limit]


def search_records(db_path: Path, query_text: str, limit: int = 5) -> list[SearchResult]:
    """Run entity-aware retrieval over prose chunks and extracted table rows."""

    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with connect(db_path) as connection:
        initialize(connection)
        return _search_records_with_connection(
            connection=connection,
            query_text=query_text,
            limit=limit,
        )


def answer_query(db_path: Path, question: str, limit: int = 10) -> QueryResponse:
    """Assemble an evidence pack for an external LLM."""

    db_path = db_path.resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    planner_result = plan_query(question)
    retrieval_tokens = _planner_retrieval_terms(planner_result.spec)
    feature_terms = _planner_feature_terms(planner_result.spec, retrieval_tokens=retrieval_tokens)
    with connect(db_path) as connection:
        initialize(connection)
        structured_candidates, prose_candidates = _collect_query_group_candidates(
            connection=connection,
            question=question,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            limit=max(limit * 5, 12),
        )
        structured_rerank_result = rerank_query_groups(
            question=question,
            planner_result=planner_result,
            candidate_groups=[
                _serialize_candidate_group_for_rerank(group)
                for group in structured_candidates
            ],
        )
        prose_rerank_result = rerank_query_groups(
            question=question,
            planner_result=planner_result,
            candidate_groups=[
                _serialize_candidate_group_for_rerank(group)
                for group in prose_candidates
            ],
        )
        structured_groups = _finalize_query_groups(
            question=question,
            planner_result=planner_result,
            candidate_groups=structured_candidates,
            rerank_result=structured_rerank_result,
            limit=limit,
        )
        prose_groups = _finalize_query_groups(
            question=question,
            planner_result=planner_result,
            candidate_groups=prose_candidates,
            rerank_result=prose_rerank_result,
            limit=limit,
        )

    coverage_notes: list[str] = []
    coverage_notes.append(planner_result.note)
    coverage_notes.append(f"Structured rerank: {structured_rerank_result.note}")
    coverage_notes.append(f"Prose rerank: {prose_rerank_result.note}")

    if planner_result.spec.intent == "feature_to_terminal":
        coverage_notes.append(
            "Query looks like a feature-to-terminal lookup; prioritized terminal-mapping and feature-summary groups."
        )
    elif planner_result.spec.intent == "terminal_lookup":
        coverage_notes.append(
            "Query looks like a terminal lookup; prioritized exact identifier groups and neighboring structured rows."
        )
    elif planner_result.spec.intent == "variant_package":
        coverage_notes.append(
            "Query looks like a variant/package lookup; prioritized ordering and package-variant groups."
        )
    elif planner_result.spec.intent == "register_control":
        coverage_notes.append(
            "Query looks like a register/control lookup; prioritized control-definition groups."
        )
    elif planner_result.spec.intent == "spec_lookup":
        coverage_notes.append(
            "Query looks like a spec lookup; prioritized electrical-spec and timing-spec groups."
        )

    if not structured_groups and not prose_groups:
        coverage_notes.append("No strong evidence group was found; consider broadening the query terms.")
    elif all(not group["items"] for group in [*structured_groups, *prose_groups]):
        coverage_notes.append("Only sparse evidence matched; additional manual inspection may still be needed.")

    family_summary = _summarize_candidate_families([*structured_candidates, *prose_candidates])
    retrieval_summary = (
        f"planner_mode: {planner_result.mode}; intent: {planner_result.spec.intent}; "
        f"structured_rerank_mode: {structured_rerank_result.mode}; "
        f"prose_rerank_mode: {prose_rerank_result.mode}; "
        f"primary_subject: {planner_result.spec.primary_subject}; "
        f"retrieval_terms: {', '.join(retrieval_tokens) or '<none>'}; "
        f"feature_terms: {', '.join(feature_terms) or '<none>'}; "
        f"must_include_terms: {', '.join(planner_result.spec.must_include_terms) or '<none>'}; "
        f"should_include_terms: {', '.join(planner_result.spec.should_include_terms) or '<none>'}; "
        f"identifier_terms: {', '.join(planner_result.spec.identifier_terms) or '<none>'}; "
        f"preferred_evidence_families: {', '.join(planner_result.spec.preferred_evidence_families) or '<none>'}; "
        f"table_family_preferences: {', '.join(planner_result.spec.table_family_preferences) or '<none>'}; "
        f"subquestions: {', '.join(planner_result.spec.subquestions) or '<none>'}; "
        f"section_hints: {', '.join(planner_result.spec.section_hints) or '<none>'}; "
        f"negative_terms: {', '.join(planner_result.spec.negative_terms) or '<none>'}; "
        f"candidate_groups: {len(structured_candidates) + len(prose_candidates)}; "
        f"selected_structured_groups: {len(structured_groups)}; "
        f"selected_prose_groups: {len(prose_groups)}; "
        f"per_channel_limit: {limit}; "
        f"candidate_family_summary: {', '.join(f'{key}={value}' for key, value in family_summary.items()) or '<none>'}"
    )
    return QueryResponse(
        question=question,
        intent=planner_result.spec.intent,
        planner_mode=planner_result.mode,
        rerank_mode=(
            structured_rerank_result.mode
            if structured_rerank_result.mode == prose_rerank_result.mode
            else f"structured={structured_rerank_result.mode}; prose={prose_rerank_result.mode}"
        ),
        primary_subject=planner_result.spec.primary_subject,
        must_include_terms=list(planner_result.spec.must_include_terms),
        should_include_terms=list(planner_result.spec.should_include_terms),
        identifier_terms=list(planner_result.spec.identifier_terms),
        table_family_preferences=list(planner_result.spec.table_family_preferences),
        preferred_evidence_families=list(planner_result.spec.preferred_evidence_families),
        subquestions=list(planner_result.spec.subquestions),
        section_hints=list(planner_result.spec.section_hints),
        negative_terms=list(planner_result.spec.negative_terms),
        retrieval_summary=retrieval_summary,
        candidate_family_summary=family_summary,
        structured_evidence_groups=structured_groups,
        prose_evidence_groups=prose_groups,
        coverage_notes=coverage_notes,
    )


def _planner_retrieval_terms(spec: QueryRetrievalSpec) -> list[str]:
    """Build retrieval terms from the planner output."""

    terms = [
        *spec.must_include_terms,
        *spec.identifier_terms,
        *spec.should_include_terms,
        *spec.section_hints,
    ]
    return _unique_preserving_order(
        term
        for term in terms
        if term
    )


def _planner_feature_terms(spec: QueryRetrievalSpec, *, retrieval_tokens: list[str]) -> list[str]:
    """Extract high-signal planner terms used for feature-oriented expansion."""

    terms = [
        *spec.must_include_terms,
        *spec.identifier_terms,
        *[term for term in spec.should_include_terms if term in QUERY_FEATURE_TERMS or " " in term],
    ]
    if spec.intent == "feature_to_terminal" and not terms:
        terms.extend(retrieval_tokens)
    return _unique_preserving_order(terms)


def _collect_query_group_candidates(
    *,
    connection: object,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    limit: int,
) -> tuple[list[QueryGroupCandidate], list[QueryGroupCandidate]]:
    """Collect family-first candidate evidence groups."""

    query_text = _build_query_seed_text(
        question=question,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
    )
    query_tokens = _normalize_tokens(question)
    normalized_query = normalize_entity_key(question)

    raw_candidates = _search_records_with_connection(
        connection=connection,
        query_text=query_text,
        limit=max(limit * 5, 50),
    )
    raw_candidates.extend(
        _entity_match_results(
            connection=connection,
            query_text=question,
            query_tokens=query_tokens,
            normalized_query=normalized_query,
        )
    )
    raw_candidates = _dedupe_results(raw_candidates)

    groups_by_key: dict[tuple[str, int, int | None], QueryGroupCandidate] = {}
    for result in raw_candidates:
        group = _build_candidate_group(
            connection=connection,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=result,
        )
        if group is None:
            continue
        group_key = (group.document_id, group.page_number, group.table_index)
        existing = groups_by_key.get(group_key)
        if existing is None or group.local_score > existing.local_score:
            groups_by_key[group_key] = group

    fallback_groups = _scan_table_groups_by_terms(
        connection=connection,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
        limit=max(limit * 3, 18),
    )
    for group in fallback_groups:
        group_key = (group.document_id, group.page_number, group.table_index)
        existing = groups_by_key.get(group_key)
        if existing is None or group.local_score > existing.local_score:
            groups_by_key[group_key] = group

    groups = list(groups_by_key.values())
    structured_groups = [
        group for group in groups
        if _evidence_group_channel(group) == "structured"
    ]
    prose_groups = [
        group for group in groups
        if _evidence_group_channel(group) == "prose"
    ]
    structured_groups = _prune_query_group_candidates(
        structured_groups,
        planner_result=planner_result,
        channel="structured",
    )
    prose_groups = _prune_query_group_candidates(
        prose_groups,
        planner_result=planner_result,
        channel="prose",
    )
    for candidate_list in (structured_groups, prose_groups):
        candidate_list.sort(
            key=lambda group: (
                -group.local_score,
                -group.quality_score,
                group.page_number,
                group.table_index if group.table_index is not None else -1,
            )
        )
    return structured_groups[: max(limit, 1)], prose_groups[: max(limit, 1)]


def _build_query_seed_text(
    *,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> str:
    """Build the broad query string used for candidate retrieval."""

    spec = planner_result.spec
    if spec.must_include_terms:
        return " ".join(spec.must_include_terms)
    if spec.intent == "generic" and retrieval_tokens:
        return " ".join(retrieval_tokens)
    if feature_terms:
        return " ".join(feature_terms)
    if spec.identifier_terms:
        return " ".join(spec.identifier_terms)
    if retrieval_tokens:
        return " ".join(retrieval_tokens)
    return question


def _build_candidate_group(
    *,
    connection: object,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
) -> QueryGroupCandidate | None:
    """Build one scored candidate group from a seed result."""

    is_prose_channel = seed.table_index is None
    if is_prose_channel and _looks_like_table_of_contents(seed.source_text):
        promoted_page_number = _find_body_page_from_navigation_chunk(
            connection=connection,
            document_id=seed.document_id,
            navigation_text=seed.source_text,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
        )
        if promoted_page_number is not None and promoted_page_number != seed.page_number:
            promoted_items = _fetch_page_chunks(
                connection=connection,
                document_id=seed.document_id,
                page_number=promoted_page_number,
            )
            if promoted_items:
                seed_candidates = _rank_prose_body_candidates(
                    promoted_items,
                    planner_result=planner_result,
                    retrieval_tokens=retrieval_tokens,
                    feature_terms=feature_terms,
                )
                if seed_candidates:
                    seed = seed_candidates[0]

    if seed.table_index is not None:
        items = _fetch_table_rows(
            connection=connection,
            document_id=seed.document_id,
            page_number=seed.page_number,
            table_index=seed.table_index,
        )
    else:
        items = _fetch_page_chunks(
            connection=connection,
            document_id=seed.document_id,
            page_number=seed.page_number,
        )
    if not items:
        return None

    evidence_family = _infer_group_evidence_family(items)
    selected_items = _select_group_items(
        items=items,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
        seed=seed,
        evidence_family=evidence_family,
    )
    if not selected_items:
        return None
    channel = "prose" if seed.table_index is None else "structured"
    quality_score = _score_group_quality(selected_items, evidence_family=evidence_family, channel=channel)
    local_score = _score_candidate_group(
        items=selected_items,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
        evidence_family=evidence_family,
        quality_score=quality_score,
        channel=channel,
    )
    if local_score <= 0:
        return None
    selected_items.sort(
        key=lambda item: (
            item.row_index if item.row_index is not None else item.chunk_index,
            item.chunk_index,
        )
    )
    summary = _build_candidate_group_summary(
        evidence_family=evidence_family,
        planner_result=planner_result,
        quality_score=quality_score,
        items=selected_items,
    )
    group_id = f"{seed.document_id}:{seed.page_number}:{seed.table_index if seed.table_index is not None else 'page'}"
    return QueryGroupCandidate(
        group_id=group_id,
        document_id=seed.document_id,
        page_number=seed.page_number,
        table_index=seed.table_index,
        section_title=selected_items[0].section_title,
        table_title=selected_items[0].table_title,
        evidence_family=evidence_family,
        local_score=local_score,
        quality_score=quality_score,
        summary=summary,
        items=selected_items,
        sample_texts=[_short_text(item.source_text) for item in selected_items[:3]],
    )


def _select_group_items(
    *,
    items: list[SearchResult],
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
    evidence_family: str,
) -> list[SearchResult]:
    """Select coherent evidence rows/chunks from inside one candidate group."""

    if seed.table_index is None:
        return _select_prose_group_items(
            items=items,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=seed,
        )

    scored: list[SearchResult] = []
    for item in items:
        score = _group_item_match_score(
            result=item,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=seed,
            evidence_family=evidence_family,
        )
        if score <= 0:
            continue
        enriched = SearchResult(**asdict(item))
        enriched.score = score
        scored.append(enriched)

    if not scored:
        return []

    intent = planner_result.spec.intent
    if evidence_family == "terminal_mapping" and intent == "feature_to_terminal":
        strong_rows = [
            item for item in scored
            if _is_strong_terminal_feature_match(
                item=item,
                planner_result=planner_result,
                retrieval_tokens=retrieval_tokens,
                feature_terms=feature_terms,
            )
        ]
        if strong_rows:
            return strong_rows
        scored.sort(key=lambda item: -item.score)
        return scored[: min(4, len(scored))]
    if evidence_family == "terminal_mapping" and intent == "terminal_lookup":
        scored.sort(key=lambda item: -item.score)
        top = scored[:1]
        if top and top[0].row_index is not None:
            primary_row = top[0].row_index
            siblings = [
                item
                for item in scored[1:]
                if item.row_index is not None and abs(item.row_index - primary_row) <= 1
            ]
            return _dedupe_results(top + siblings)
        return top
    if evidence_family in {"package_variant", "ordering_info", "electrical_spec", "timing_spec", "control_definition", "feature_summary"}:
        return scored[: min(8, len(scored))]
    return scored[: min(4, len(scored))]


def _select_prose_group_items(
    *,
    items: list[SearchResult],
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
) -> list[SearchResult]:
    """Select explanatory prose items from one page-level candidate group."""

    del seed
    ranked = _rank_prose_body_candidates(
        items,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
    )
    if ranked:
        return ranked[: min(4, len(ranked))]
    if any(_looks_like_table_of_contents(item.source_text) for item in items):
        toc_items = []
        for item in items:
            if not _looks_like_table_of_contents(item.source_text):
                continue
            enriched = SearchResult(**asdict(item))
            enriched.score = 1.0
            toc_items.append(enriched)
        return toc_items[:1]
    return []


def _rank_prose_body_candidates(
    items: list[SearchResult],
    *,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> list[SearchResult]:
    """Rank body-prose candidates with prose-specific scoring."""

    scored: list[SearchResult] = []
    for item in items:
        prose_score = _prose_item_match_score(
            result=item,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
        )
        if prose_score <= 0:
            continue
        enriched = SearchResult(**asdict(item))
        enriched.score = prose_score
        scored.append(enriched)
    scored.sort(key=lambda item: (-item.score, item.chunk_index))
    return scored


def _is_strong_terminal_feature_match(
    *,
    item: SearchResult,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> bool:
    """Require terminal rows to carry direct signal evidence for feature-to-terminal queries."""

    signal_terms = [
        *planner_result.spec.identifier_terms,
        *planner_result.spec.must_include_terms,
        *feature_terms,
    ]
    if _result_matches_terms(item, signal_terms):
        return True

    cells = item.cells or []
    focused_text = " ".join(
        value
        for index, value in enumerate(cells)
        if value and index in {1, 2, 3, 4}
    ).lower()
    for term in planner_result.spec.identifier_terms:
        if term.lower() in focused_text:
            return True
    for term in planner_result.spec.must_include_terms:
        normalized = _normalize_text(term)
        if normalized and normalized in _normalize_text(focused_text):
            return True
    for term in feature_terms:
        normalized = _normalize_text(term)
        if normalized and normalized in _normalize_text(focused_text):
            return True
    return False


def _group_item_match_score(
    *,
    result: SearchResult,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
    evidence_family: str,
) -> float:
    """Score one row/chunk for inclusion inside a candidate group."""

    score = 0.0
    if result.chunk_type == "table_row":
        score += 15.0
    if result.page_number == seed.page_number and result.table_index == seed.table_index:
        score += 20.0
    if _result_matches_terms(result, planner_result.spec.identifier_terms):
        score += 140.0
    if _result_matches_terms(result, planner_result.spec.must_include_terms):
        score += 120.0
    if _result_matches_terms(result, feature_terms):
        score += 90.0
    elif _result_matches_terms(result, retrieval_tokens):
        score += 35.0
    if _result_matches_terms(result, planner_result.spec.section_hints):
        score += 30.0
    if _result_matches_terms(result, planner_result.spec.negative_terms):
        score -= 160.0
    if evidence_family == "terminal_mapping" and _looks_like_terminal_headers(result.headers or []):
        score += 60.0
    if evidence_family == "control_definition" and result.entity_family == "register_or_control_item":
        score += 45.0
    if evidence_family in {"electrical_spec", "timing_spec"} and result.entity_family == "spec_item":
        score += 45.0
    if evidence_family in {"package_variant", "ordering_info"} and result.entity_family == "component":
        score += 45.0
    if _looks_like_long_descriptive_entity(result):
        score -= 120.0
    return score


def _score_candidate_group(
    *,
    items: list[SearchResult],
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    evidence_family: str,
    quality_score: float,
    channel: str,
) -> float:
    """Score one candidate group before reranking."""

    score = max(item.score for item in items)
    score += quality_score * 140.0
    score += _evidence_family_priority(evidence_family, planner_result=planner_result)
    if evidence_family in planner_result.spec.preferred_evidence_families:
        score += 110.0
    elif planner_result.spec.preferred_evidence_families:
        score -= 20.0
    if evidence_family == "bom_like" and planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup", "spec_lookup"}:
        score -= 220.0
    if evidence_family == "mechanical_info" and planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup", "spec_lookup"}:
        score -= 220.0
    if evidence_family == "application_circuit" and planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup"}:
        score -= 120.0
    if feature_terms and any(_result_matches_terms(item, feature_terms) for item in items):
        score += 35.0
    if retrieval_tokens and any(_result_matches_terms(item, retrieval_tokens) for item in items):
        score += 12.0
    if channel == "prose":
        if any(_looks_like_table_of_contents(item.source_text) for item in items):
            score -= 280.0
        if any(_looks_like_explanatory_prose(item.source_text) for item in items):
            score += 90.0
        if any(_looks_like_section_heading(item.source_text, " ".join(retrieval_tokens[:3])) for item in items if retrieval_tokens):
            score += 20.0
    return score


def _evidence_family_priority(evidence_family: str, *, planner_result: QueryPlannerResult) -> float:
    """Return intent-specific family priorities."""

    priorities: dict[str, list[str]] = {
        "feature_to_terminal": ["terminal_mapping", "feature_summary", "application_circuit"],
        "terminal_lookup": ["terminal_mapping", "feature_summary"],
        "variant_package": ["package_variant", "ordering_info", "generic_text"],
        "spec_lookup": ["electrical_spec", "timing_spec", "feature_summary"],
        "register_control": ["control_definition", "feature_summary"],
        "generic": planner_result.spec.preferred_evidence_families or ["generic_text"],
    }
    family_order = priorities.get(planner_result.spec.intent, planner_result.spec.preferred_evidence_families)
    if evidence_family in family_order:
        position = family_order.index(evidence_family)
        return 100.0 - (position * 25.0)
    return 0.0


def _score_group_quality(items: list[SearchResult], *, evidence_family: str, channel: str) -> float:
    """Estimate whether a candidate group is coherent enough for query output."""

    if not items:
        return 0.0
    if channel == "prose":
        return _score_prose_group_quality(items)
    score = 0.35
    first = items[0]
    if first.headers:
        non_empty_headers = [header for header in first.headers if header]
        score += min(len(non_empty_headers), 6) * 0.04
        if _looks_like_terminal_headers(first.headers):
            score += 0.12
    row_texts = [item.source_text for item in items if item.source_text]
    if row_texts:
        avg_len = sum(len(text) for text in row_texts) / len(row_texts)
        if avg_len < 220:
            score += 0.12
        elif avg_len > 700:
            score -= 0.25
    noisy_rows = 0
    for item in items:
        text = item.source_text or ""
        if len(text) > 300 and re.search(r"(?:[A-Z0-9]\s){12,}", text):
            noisy_rows += 1
        if "Â" in text or "�" in text:
            noisy_rows += 1
    score -= min(noisy_rows, 3) * 0.12
    if evidence_family in {"bom_like", "mechanical_info"}:
        score -= 0.08
    return max(0.0, min(score, 1.0))


def _score_prose_group_quality(items: list[SearchResult]) -> float:
    """Estimate whether a prose group is explanatory body text rather than navigation text."""

    if not items:
        return 0.0
    score = 0.28
    explanatory_count = 0
    toc_count = 0
    heading_count = 0
    for item in items:
        text = item.source_text or ""
        if _looks_like_table_of_contents(text):
            toc_count += 1
        if _looks_like_explanatory_prose(text):
            explanatory_count += 1
        if _looks_like_dense_heading_listing(text):
            heading_count += 1
        if len(text) > 220:
            score += 0.04
    score += min(explanatory_count, 3) * 0.18
    score -= min(toc_count, 3) * 0.35
    score -= min(heading_count, 3) * 0.16
    return max(0.0, min(score, 1.0))


def _infer_group_evidence_family(items: list[SearchResult]) -> str:
    """Infer a generic electronics evidence family for one candidate group."""

    first = items[0]
    header_set = {
        re.sub(r"\s+", " ", header).strip().lower()
        for header in (first.headers or [])
        if header
    }
    title_blob = " ".join(
        part.lower()
        for part in [first.section_title or "", first.table_title or "", first.source_text or ""]
        if part
    )
    if {"designator", "value", "footprint"} & header_set or "bill of material" in title_blob or "bom" in title_blob:
        return "bom_like"
    if any(term in title_blob for term in ("package dimensions", "mechanical", "land pattern", "outline", "footprint drawing")):
        return "mechanical_info"
    if any(term in title_blob for term in ("application circuit", "circuit configuration", "reference design", "schematic")):
        return "application_circuit"
    if _looks_like_terminal_headers(first.headers or []):
        return "terminal_mapping"
    if {"part number", "variant", "package"} & header_set:
        return "package_variant"
    if {"ordering information", "order code"} & header_set or "ordering information" in title_blob:
        return "ordering_info"
    if {"parameter", "min", "typ", "max", "unit"} & header_set:
        if any(term in title_blob for term in ("timing", "propagation", "delay", "switching", "frequency")):
            return "timing_spec"
        return "electrical_spec"
    if {"mode", "state", "input", "output", "function"} & header_set or "truth table" in title_blob:
        return "feature_summary"
    if {"id", "field", "description"}.issubset(header_set) or any(
        term in title_blob for term in ("register", "address offset", "subscribe configuration", "command register")
    ):
        return "control_definition"
    return "generic_text"


def _build_candidate_group_summary(
    *,
    evidence_family: str,
    planner_result: QueryPlannerResult,
    quality_score: float,
    items: list[SearchResult],
) -> str:
    """Build a compact summary for one candidate group."""

    return (
        f"{evidence_family} evidence; quality={quality_score:.2f}; "
        f"items={len(items)}; intent={planner_result.spec.intent}; planner_mode={planner_result.mode}"
    )


def _serialize_candidate_group_for_rerank(group: QueryGroupCandidate) -> dict[str, object]:
    """Serialize a candidate group into the compact rerank payload shape."""

    return {
        "group_id": group.group_id,
        "evidence_family": group.evidence_family,
        "quality_score": round(group.quality_score, 4),
        "local_score": round(group.local_score, 4),
        "section_title": group.section_title,
        "table_title": group.table_title,
        "summary": group.summary,
        "sample_texts": list(group.sample_texts),
    }


def _finalize_query_groups(
    *,
    question: str,
    planner_result: QueryPlannerResult,
    candidate_groups: list[QueryGroupCandidate],
    rerank_result: QueryGroupRerankResult,
    limit: int,
) -> list[dict[str, object]]:
    """Finalize evidence groups after reranking."""

    del question
    groups_by_id = {group.group_id: group for group in candidate_groups}
    ordered_groups: list[QueryGroupCandidate] = []
    for group_id in rerank_result.ranked_group_ids:
        group = groups_by_id.get(group_id)
        if group is not None:
            ordered_groups.append(group)
    if not ordered_groups:
        ordered_groups = list(candidate_groups)
    ordered_groups = _apply_subquestion_coverage_selection(
        ordered_groups,
        planner_result=planner_result,
        rerank_result=rerank_result,
        limit=limit,
    )
    return [
        {
            "page_number": group.page_number,
            "table_index": group.table_index,
            "section_title": group.section_title,
            "table_title": group.table_title,
            "summary": group.summary,
            "group_score": group.local_score,
            "quality_score": group.quality_score,
            "evidence_family": group.evidence_family,
            "rerank_reason": rerank_result.reason_codes.get(group.group_id),
            "items": [_serialize_query_item(item) for item in group.items],
        }
        for group in ordered_groups[:limit]
    ]


def _apply_subquestion_coverage_selection(
    ordered_groups: list[QueryGroupCandidate],
    *,
    planner_result: QueryPlannerResult,
    rerank_result: QueryGroupRerankResult,
    limit: int,
) -> list[QueryGroupCandidate]:
    """Broaden final group selection so distinct planner subgoals are covered when possible."""

    coverage_targets = _build_subquestion_coverage_targets(planner_result)
    if not coverage_targets or not ordered_groups:
        return ordered_groups

    selected: list[QueryGroupCandidate] = []
    selected_ids: set[str] = set()
    covered_keys: set[str] = set()

    for target_key, target_terms in coverage_targets:
        existing = next(
            (
                group
                for group in selected
                if _group_matches_coverage_terms(
                    group,
                    target_terms=target_terms,
                    planner_result=planner_result,
                    rerank_result=rerank_result,
                )
            ),
            None,
        )
        if existing is not None:
            covered_keys.add(target_key)
            continue

        match = next(
            (
                group
                for group in ordered_groups
                if group.group_id not in selected_ids
                and _group_matches_coverage_terms(
                    group,
                    target_terms=target_terms,
                    planner_result=planner_result,
                    rerank_result=rerank_result,
                )
            ),
            None,
        )
        if match is not None:
            selected.append(match)
            selected_ids.add(match.group_id)
            covered_keys.add(target_key)
            if len(selected) >= limit:
                break

    for group in ordered_groups:
        if len(selected) >= limit:
            break
        if group.group_id in selected_ids:
            continue
        selected.append(group)
        selected_ids.add(group.group_id)

    if not selected:
        return ordered_groups
    return selected + [
        group for group in ordered_groups
        if group.group_id not in selected_ids
    ]


def _build_subquestion_coverage_targets(
    planner_result: QueryPlannerResult,
) -> list[tuple[str, list[str]]]:
    """Build deduplicated coverage targets from planner subquestions."""

    spec = planner_result.spec
    targets: dict[str, list[str]] = {}
    for subquestion in spec.subquestions:
        key, terms = _subquestion_coverage_profile(subquestion, planner_result=planner_result)
        if not key or not terms:
            continue
        existing = targets.setdefault(key, [])
        for term in terms:
            if term not in existing:
                existing.append(term)
    return [(key, terms) for key, terms in targets.items()]


def _subquestion_coverage_profile(
    subquestion: str,
    *,
    planner_result: QueryPlannerResult,
) -> tuple[str, list[str]]:
    """Create one coverage profile from a planner subquestion."""

    normalized = _normalize_text(subquestion)
    spec = planner_result.spec
    if spec.intent == "feature_to_terminal":
        if any(token in normalized for token in ("lf", "lfxo", "low frequency", "32 768", "32.768", "xl1", "xl2")):
            return (
                "lf_cluster",
                _select_cluster_terms(
                    planner_result,
                    include_patterns=(r"\blf\b", r"lfxo", r"low frequency", r"32\.?768", r"\bxl\d\b"),
                ),
            )
        if any(token in normalized for token in ("hf", "hfxo", "high frequency", "xc1", "xc2", "xtal1", "xtal2", "xin", "xout")):
            return (
                "hf_cluster",
                _select_cluster_terms(
                    planner_result,
                    include_patterns=(r"\bhf\b", r"hfxo", r"high frequency", r"\bxc\d\b", r"32 mhz", r"xtal", r"\bxin\b", r"\bxout\b", r"osc_in", r"osc_out"),
                ),
            )
    generic_terms = _unique_preserving_order(_normalize_tokens(subquestion))
    return (normalize_entity_key(subquestion), generic_terms)


def _select_cluster_terms(
    planner_result: QueryPlannerResult,
    *,
    include_patterns: tuple[str, ...],
) -> list[str]:
    """Select planner terms belonging to one subquestion cluster."""

    candidates = [
        *planner_result.spec.identifier_terms,
        *planner_result.spec.must_include_terms,
        *planner_result.spec.should_include_terms,
        *planner_result.spec.section_hints,
    ]
    matched: list[str] = []
    for candidate in candidates:
        lowered = candidate.lower()
        normalized = _normalize_text(candidate)
        for pattern in include_patterns:
            if re.search(pattern, lowered) or re.search(pattern, normalized):
                if candidate not in matched:
                    matched.append(candidate)
                break
    return matched


def _group_matches_coverage_terms(
    group: QueryGroupCandidate,
    *,
    target_terms: list[str],
    planner_result: QueryPlannerResult,
    rerank_result: QueryGroupRerankResult,
) -> bool:
    """Check whether a candidate group covers one planner subgoal."""

    if not target_terms:
        return False
    reason_code = (rerank_result.reason_codes.get(group.group_id) or "").lower()
    if reason_code:
        for term in target_terms:
            lowered = term.lower()
            normalized = _normalize_text(term)
            if lowered in reason_code or (normalized and normalized in _normalize_text(reason_code)):
                return True
        if any(term in reason_code for term in ("lf_crystal", "lfxo", "xl1", "xl2")) and any(
            re.search(pattern, " ".join(target_terms).lower())
            for pattern in (r"\blf\b", r"lfxo", r"xl1", r"xl2")
        ):
            return True
        if any(term in reason_code for term in ("hf_crystal", "hfxo", "xc1", "xc2")) and any(
            re.search(pattern, " ".join(target_terms).lower())
            for pattern in (r"\bhf\b", r"hfxo", r"xc1", r"xc2")
        ):
            return True
    return any(
        _result_matches_terms(item, target_terms)
        for item in group.items
    )


def _summarize_candidate_families(groups: list[QueryGroupCandidate]) -> dict[str, int]:
    """Summarize candidate counts by evidence family."""

    counts: dict[str, int] = {}
    for group in groups:
        counts[group.evidence_family] = counts.get(group.evidence_family, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _prune_query_group_candidates(
    groups: list[QueryGroupCandidate],
    *,
    planner_result: QueryPlannerResult,
    channel: str,
) -> list[QueryGroupCandidate]:
    """Apply hard gating so the query path avoids clearly wrong evidence classes."""

    if not groups:
        return groups
    preferred = planner_result.spec.preferred_evidence_families
    strong_preferred = [
        group for group in groups
        if group.evidence_family in preferred and group.quality_score >= 0.32
    ]
    if strong_preferred:
        intent = planner_result.spec.intent
        if intent in {"feature_to_terminal", "terminal_lookup", "variant_package", "spec_lookup"}:
            groups = [
                group for group in groups
                if group.evidence_family in preferred
                or group.local_score >= max(item.local_score for item in strong_preferred) - 45.0
            ]

    best_quality_by_family: dict[str, float] = {}
    for group in groups:
        best_quality_by_family[group.evidence_family] = max(
            group.quality_score,
            best_quality_by_family.get(group.evidence_family, 0.0),
        )

    pruned: list[QueryGroupCandidate] = []
    for group in groups:
        if group.evidence_family in {"bom_like", "mechanical_info"} and planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup", "spec_lookup"}:
            continue
        if channel == "prose":
            if group.quality_score < 0.2:
                continue
            if all(_looks_like_table_of_contents(item.source_text) for item in group.items if item.source_text):
                continue
        if channel != "prose" and group.evidence_family == "generic_text" and strong_preferred and group.evidence_family not in preferred:
            continue
        if channel != "prose" and group.evidence_family == "application_circuit" and strong_preferred and planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup"}:
            continue
        if (
            group.quality_score < 0.16
            and best_quality_by_family.get(group.evidence_family, 0.0) >= 0.32
        ):
            continue
        pruned.append(group)
    return pruned or groups


def _evidence_group_channel(group: QueryGroupCandidate) -> str:
    """Classify a candidate group into the structured or prose evidence channel."""

    if group.table_index is None:
        return "prose"
    return "structured"


def _scan_table_groups_by_terms(
    *,
    connection: object,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    limit: int,
) -> list[QueryGroupCandidate]:
    """Fallback scan over stored table groups using section/title/row-term matches."""

    candidate_terms = feature_terms or retrieval_tokens
    if not candidate_terms:
        return []
    rows = connection.execute(
        """
        SELECT document_id, page_number, table_index
        FROM tables
        ORDER BY page_number ASC, table_index ASC
        """
    ).fetchall()
    groups: list[QueryGroupCandidate] = []
    for row in rows:
        items = _fetch_table_rows(
            connection=connection,
            document_id=row["document_id"],
            page_number=row["page_number"],
            table_index=row["table_index"],
        )
        if not items or not any(_result_matches_terms(item, candidate_terms) for item in items):
            continue
        seed = items[0]
        group = _build_candidate_group(
            connection=connection,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=seed,
        )
        if group is not None:
            groups.append(group)
        if len(groups) >= limit:
            break
    return groups


def _short_text(text: str, *, limit: int = 220) -> str:
    """Shorten one evidence snippet for rerank prompts."""

    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _query_seed_records(
    *,
    connection: object,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    limit: int,
) -> list[SearchResult]:
    """Gather and rerank seed records for the query-only evidence flow."""

    intent = planner_result.spec.intent
    if planner_result.spec.must_include_terms:
        query_text = " ".join(planner_result.spec.must_include_terms)
    elif feature_terms:
        query_text = " ".join(feature_terms)
    elif intent == "terminal_lookup":
        identifiers = re.findall(r"[A-Za-z]+\d+(?:[./]\d+)?", question)
        query_text = identifiers[0] if identifiers else (" ".join(retrieval_tokens) if retrieval_tokens else question)
    elif intent == "variant_package":
        query_text = " ".join(
            token for token in retrieval_tokens if token not in {"part", "number", "package", "variant", "ordering"}
        ) or " ".join(retrieval_tokens)
    else:
        query_text = " ".join(retrieval_tokens) if retrieval_tokens else question
    candidate_results = _search_records_with_connection(
        connection=connection,
        query_text=query_text,
        limit=max(limit * 4, 30),
    )
    reranked: list[SearchResult] = []
    for result in candidate_results:
        adjusted = SearchResult(**asdict(result))
        adjusted.score = result.score + _query_seed_bonus(
            result=result,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
        )
        reranked.append(adjusted)
    reranked = _dedupe_results(reranked)
    reranked.sort(
        key=lambda item: (
            -item.score,
            item.page_number,
            item.table_index if item.table_index is not None else -1,
            item.row_index if item.row_index is not None else item.chunk_index,
        )
    )
    return _diverse_seed_selection(reranked, limit=limit)


def _build_query_evidence_groups(
    *,
    connection: object,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seeds: list[SearchResult],
    limit: int,
) -> list[dict[str, object]]:
    """Expand seed records into grouped evidence bundles."""

    groups: list[dict[str, object]] = []
    seen_groups: set[tuple[int, int | None]] = set()
    for seed in seeds:
        group_key = (seed.page_number, seed.table_index)
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        group = _expand_seed_group(
            connection=connection,
            question=question,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=seed,
        )
        if group is None:
            continue
        groups.append(group)

    groups.sort(
        key=lambda group: (
            -float(group["group_score"]),
            int(group["page_number"]),
            -1 if group["table_index"] is None else int(group["table_index"]),
        )
    )
    if planner_result.spec.intent in {"feature_to_terminal", "terminal_lookup"}:
        table_groups = [group for group in groups if group["table_index"] is not None]
        if table_groups:
            groups = table_groups
    if groups:
        return groups[:limit]
    fallback_groups = _grep_fallback_groups(
        connection=connection,
        question=question,
        planner_result=planner_result,
        retrieval_tokens=retrieval_tokens,
        feature_terms=feature_terms,
        limit=limit,
    )
    return fallback_groups[:limit]


def _expand_seed_group(
    *,
    connection: object,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
) -> dict[str, object] | None:
    """Expand one seed into a region-aware evidence group."""

    if seed.table_index is not None:
        candidate_rows = _fetch_table_rows(
            connection=connection,
            document_id=seed.document_id,
            page_number=seed.page_number,
            table_index=seed.table_index,
        )
        matching_rows: list[SearchResult] = []
        for row in candidate_rows:
            expansion_score = _query_expansion_bonus(
                result=row,
                planner_result=planner_result,
                retrieval_tokens=retrieval_tokens,
                feature_terms=feature_terms,
                seed=seed,
            )
            if expansion_score <= 0:
                continue
            expanded = SearchResult(**asdict(row))
            expanded.score = row.score + expansion_score
            matching_rows.append(expanded)

        if not matching_rows:
            matching_rows = [seed]

        matching_rows = _dedupe_results(matching_rows)
        matching_rows.sort(key=lambda item: (item.row_index if item.row_index is not None else item.chunk_index, item.page_number))
        summary = _build_group_summary(
            seed=seed,
            planner_result=planner_result,
            feature_terms=feature_terms,
            item_count=len(matching_rows),
        )
        return {
            "page_number": seed.page_number,
            "table_index": seed.table_index,
            "section_title": seed.section_title,
            "table_title": seed.table_title,
            "summary": summary,
            "group_score": max(item.score for item in matching_rows),
            "items": [_serialize_query_item(item) for item in matching_rows],
        }

    expanded_items = [seed]
    page_matches = _fetch_page_chunks(
        connection=connection,
        document_id=seed.document_id,
        page_number=seed.page_number,
    )
    for item in page_matches:
        expansion_score = _query_expansion_bonus(
            result=item,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=seed,
        )
        if expansion_score <= 0:
            continue
        expanded = SearchResult(**asdict(item))
        expanded.score = item.score + expansion_score
        expanded_items.append(expanded)
    expanded_items = _dedupe_results(expanded_items)
    expanded_items.sort(key=lambda item: (item.chunk_index, item.page_number))
    summary = _build_group_summary(
        seed=seed,
        planner_result=planner_result,
        feature_terms=feature_terms,
        item_count=len(expanded_items),
    )
    return {
        "page_number": seed.page_number,
        "table_index": seed.table_index,
        "section_title": seed.section_title,
        "table_title": seed.table_title,
        "summary": summary,
        "group_score": max(item.score for item in expanded_items),
        "items": [_serialize_query_item(item) for item in expanded_items],
    }


def _grep_fallback_groups(
    *,
    connection: object,
    question: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    limit: int,
) -> list[dict[str, object]]:
    """Fallback grep-style grouping when the seed pass is too weak."""

    intent = planner_result.spec.intent
    candidate_terms = feature_terms or retrieval_tokens
    if not candidate_terms:
        return []
    rows = connection.execute(
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
            table_rows.text_rendering,
            tables.crop_path
        FROM table_rows
        LEFT JOIN tables
          ON tables.document_id = table_rows.document_id
         AND tables.page_number = table_rows.page_number
         AND tables.table_index = table_rows.table_index
        ORDER BY table_rows.page_number ASC, table_rows.table_index ASC, table_rows.row_index ASC
        """
    ).fetchall()

    grouped: dict[tuple[int, int], list[SearchResult]] = {}
    for row in rows:
        result = SearchResult(
            document_id=row["document_id"],
            page_number=row["page_number"],
            chunk_index=row["row_index"],
            chunk_type=row["chunk_type"],
            source_text=row["text_rendering"],
            bm25_score=0.0,
            score=0.0,
            table_index=row["table_index"],
            row_index=row["row_index"],
            table_title=row["table_title"],
            section_title=row["section_title"],
            headers=json.loads(row["headers_json"]),
            cells=json.loads(row["cells_json"]),
            row_type=row["row_type"],
            crop_path=row["crop_path"],
        )
        if not _result_matches_terms(result, candidate_terms):
            continue
        bonus = _query_expansion_bonus(
            result=result,
            planner_result=planner_result,
            retrieval_tokens=retrieval_tokens,
            feature_terms=feature_terms,
            seed=result,
        )
        if bonus <= 0:
            continue
        result.score = bonus
        grouped.setdefault((result.page_number, result.table_index or 0), []).append(result)

    groups = []
    for (_page_number, _table_index), items in grouped.items():
        items.sort(key=lambda item: item.row_index if item.row_index is not None else item.chunk_index)
        groups.append(
            {
                "page_number": items[0].page_number,
                "table_index": items[0].table_index,
                "section_title": items[0].section_title,
                "table_title": items[0].table_title,
                "summary": _build_group_summary(
                    seed=items[0],
                    planner_result=planner_result,
                    feature_terms=feature_terms,
                    item_count=len(items),
                ),
                "group_score": max(item.score for item in items),
                "items": [_serialize_query_item(item) for item in items],
            }
        )
    groups.sort(key=lambda group: (-float(group["group_score"]), int(group["page_number"])))
    return groups[:limit]


def evaluate_queries(db_path: Path, eval_file: Path) -> dict[str, object]:
    """Run the compact evaluation harness over search and grounded query."""

    cases = load_eval_cases(eval_file)
    return evaluate_cases(
        cases=cases,
        search_fn=lambda query_text, limit: search_records(db_path=db_path, query_text=query_text, limit=limit),
        query_fn=lambda question: answer_query(db_path=db_path, question=question),
    )


def _entity_match_results(
    *,
    connection: object,
    query_text: str,
    query_tokens: list[str],
    normalized_query: str,
) -> list[SearchResult]:
    """Recover evidence rows/chunks directly from matched entities."""

    entity_rows = connection.execute(
        """
        SELECT entity_key, entity_family, normalized_key, display_text, aliases_json, confidence
        FROM entities
        ORDER BY document_id ASC, entity_family ASC, display_text ASC
        """
    ).fetchall()

    matched_entities: dict[str, tuple[float, str, str]] = {}
    query_lower = query_text.strip().lower()
    for row in entity_rows:
        aliases = json.loads(row["aliases_json"])
        score = _entity_match_score(
            query_lower=query_lower,
            query_tokens=query_tokens,
            normalized_query=normalized_query,
            normalized_key=row["normalized_key"],
            display_text=row["display_text"],
            aliases=aliases,
            entity_family=row["entity_family"],
        )
        if score < 80.0:
            continue
        matched_entities[row["entity_key"]] = (
            score * float(row["confidence"]),
            row["entity_family"],
            row["display_text"],
        )

    if not matched_entities:
        return []

    evidence_rows = connection.execute(
        """
        SELECT
            entity_evidence.document_id,
            entity_evidence.entity_key,
            entity_evidence.page_number,
            entity_evidence.source_kind,
            entity_evidence.table_index,
            entity_evidence.row_index,
            entity_evidence.chunk_index,
            entity_evidence.evidence_text,
            entity_evidence.confidence,
            table_rows.chunk_type AS table_chunk_type,
            table_rows.table_title,
            table_rows.section_title,
            table_rows.headers_json,
            table_rows.cells_json,
            table_rows.row_type,
            tables.crop_path,
            chunks.chunk_type AS chunk_chunk_type
        FROM entity_evidence
        LEFT JOIN table_rows
          ON table_rows.document_id = entity_evidence.document_id
         AND table_rows.page_number = entity_evidence.page_number
         AND table_rows.table_index = entity_evidence.table_index
         AND table_rows.row_index = entity_evidence.row_index
        LEFT JOIN tables
          ON tables.document_id = entity_evidence.document_id
         AND tables.page_number = entity_evidence.page_number
         AND tables.table_index = entity_evidence.table_index
        LEFT JOIN chunks
          ON chunks.document_id = entity_evidence.document_id
         AND chunks.page_number = entity_evidence.page_number
         AND chunks.chunk_index = entity_evidence.chunk_index
        ORDER BY entity_evidence.page_number ASC
        """
    ).fetchall()

    results: list[SearchResult] = []
    for row in evidence_rows:
        entity_match = matched_entities.get(row["entity_key"])
        if entity_match is None:
            continue
        entity_score, entity_family, entity_display = entity_match
        if row["source_kind"] == "document_metadata" and entity_score < 180.0:
            continue
        score = entity_score + (float(row["confidence"]) * 120.0)
        if row["source_kind"] == "table_row":
            headers = json.loads(row["headers_json"]) if row["headers_json"] else None
            cells = json.loads(row["cells_json"]) if row["cells_json"] else None
            results.append(
                SearchResult(
                    document_id=row["document_id"],
                    page_number=row["page_number"],
                    chunk_index=row["row_index"] if row["row_index"] is not None else 0,
                    chunk_type=row["table_chunk_type"] or "table_row",
                    source_text=row["evidence_text"],
                    bm25_score=0.0,
                    score=score + _entity_family_bonus(entity_family, query_text),
                    table_index=row["table_index"],
                    row_index=row["row_index"],
                    table_title=row["table_title"],
                    section_title=row["section_title"],
                    headers=headers,
                    cells=cells,
                    row_type=row["row_type"],
                    crop_path=row["crop_path"],
                    entity_family=entity_family,
                    entity_display_text=entity_display,
                )
            )
        else:
            results.append(
                SearchResult(
                    document_id=row["document_id"],
                    page_number=row["page_number"],
                    chunk_index=row["chunk_index"] if row["chunk_index"] is not None else 0,
                    chunk_type=row["chunk_chunk_type"] or row["source_kind"],
                    source_text=row["evidence_text"],
                    bm25_score=0.0,
                    score=score + _entity_family_bonus(entity_family, query_text),
                    entity_family=entity_family,
                    entity_display_text=entity_display,
                )
            )
    return results


def _entity_match_score(
    *,
    query_lower: str,
    query_tokens: list[str],
    normalized_query: str,
    normalized_key: str,
    display_text: str,
    aliases: list[str],
    entity_family: str,
) -> float:
    """Score a query against one entity and its aliases."""

    corpus = [display_text, *aliases]
    corpus_lower = [value.lower() for value in corpus]
    corpus_compact = [normalize_entity_key(value) for value in corpus]
    best = 0.0

    if normalized_query and normalized_query == normalized_key:
        best = max(best, 240.0)
    if normalized_query and normalized_query in corpus_compact:
        best = max(best, 225.0)
    if query_lower and query_lower in corpus_lower:
        best = max(best, 205.0)

    for value in corpus_lower:
        if query_lower and query_lower in value:
            best = max(best, 165.0)

    token_hits = 0
    token_corpus = _normalize_text(" ".join(corpus))
    for token in query_tokens:
        if token in token_corpus:
            token_hits += 1
    if query_tokens:
        best = max(best, (token_hits / len(query_tokens)) * 140.0)

    best += _entity_family_bonus(entity_family, query_lower)
    return best


def _entity_family_bonus(entity_family: str, query_text: str) -> float:
    """Prefer entity families that match the query shape."""

    query_lower = query_text.lower()
    if re.search(r"[A-Za-z]+\d+(?:[./\[]\d+)?", query_text):
        if entity_family in {"signal_or_terminal", "register_or_control_item", "component"}:
            return 60.0
    if any(keyword in query_lower for keyword in ["pin", "ball", "pad", "terminal", "function"]):
        if entity_family == "signal_or_terminal":
            return 45.0
    if any(keyword in query_lower for keyword in ["part number", "package", "variant", "ordering"]):
        if entity_family == "component":
            return 45.0
    if any(keyword in query_lower for keyword in ["voltage", "current", "timing", "frequency", "accuracy", "temperature"]):
        if entity_family == "spec_item":
            return 40.0
    return 0.0


def _query_seed_bonus(
    *,
    result: SearchResult,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> float:
    """Apply query-only reranking to candidate seed results."""

    intent = planner_result.spec.intent
    bonus = 0.0
    if result.chunk_type == "table_row":
        bonus += 55.0
    if result.headers and _looks_like_terminal_headers(result.headers):
        bonus += 35.0
    if result.section_title and any(term in result.section_title.lower() for term in ("pin", "assignment", "package")):
        bonus += 20.0
    if result.table_title and any(term in result.table_title.lower() for term in ("pin", "assignment", "package")):
        bonus += 15.0

    result_table_family = _infer_result_table_family(result)
    if result_table_family and result_table_family in planner_result.spec.table_family_preferences:
        bonus += 80.0
    elif planner_result.spec.table_family_preferences:
        bonus -= 15.0
    if _result_matches_terms(result, planner_result.spec.section_hints):
        bonus += 45.0
    if _result_matches_terms(result, planner_result.spec.negative_terms):
        bonus -= 140.0

    if intent == "feature_to_terminal":
        if result.chunk_type == "table_row" and _looks_like_terminal_headers(result.headers or []):
            bonus += 120.0
        if result.entity_family == "signal_or_terminal":
            bonus += 85.0
        if result.entity_family == "register_or_control_item":
            bonus -= 160.0
        if result.entity_family == "component":
            bonus -= 40.0
        if _result_matches_terms(result, feature_terms):
            bonus += 95.0
        elif result.chunk_type != "table_row":
            bonus -= 140.0

    elif intent == "terminal_lookup":
        if result.entity_family == "signal_or_terminal":
            bonus += 90.0
        if result.chunk_type == "table_row":
            bonus += 40.0
        if _looks_like_terminal_headers(result.headers or []):
            bonus += 50.0

    elif intent == "variant_package":
        if result.entity_family == "component":
            bonus += 70.0
        if result.entity_family == "spec_item":
            bonus += 30.0
        if result.chunk_type == "table_row":
            bonus += 30.0

    elif intent == "register_control":
        if result.entity_family == "register_or_control_item":
            bonus += 90.0
        if result.chunk_type == "table_row":
            bonus += 35.0

    elif intent == "spec_lookup":
        if result.entity_family == "spec_item":
            bonus += 80.0
        if result.chunk_type == "table_row":
            bonus += 35.0

    if _looks_like_long_descriptive_entity(result):
        if intent in {"feature_to_terminal", "terminal_lookup", "variant_package"}:
            bonus -= 220.0
        else:
            bonus -= 110.0

    if planner_result.spec.identifier_terms and _result_matches_terms(result, planner_result.spec.identifier_terms):
        bonus += 110.0
    if planner_result.spec.must_include_terms and _result_matches_terms(result, planner_result.spec.must_include_terms):
        bonus += 95.0
    if retrieval_tokens and _result_matches_terms(result, retrieval_tokens):
        bonus += 20.0
    return bonus


def _query_expansion_bonus(
    *,
    result: SearchResult,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
    seed: SearchResult,
) -> float:
    """Score a row or chunk for inclusion inside an evidence group."""

    intent = planner_result.spec.intent
    bonus = 0.0
    if result.table_index == seed.table_index and result.page_number == seed.page_number:
        bonus += 40.0
    if result.chunk_type == "table_row":
        bonus += 25.0
    if _result_matches_terms(result, feature_terms):
        bonus += 80.0
    elif _result_matches_terms(result, retrieval_tokens):
        bonus += 35.0
    if intent in {"feature_to_terminal", "terminal_lookup"} and _looks_like_terminal_headers(result.headers or []):
        bonus += 60.0
    if (
        seed.row_index is not None
        and result.row_index is not None
        and abs(result.row_index - seed.row_index) <= 2
        and result.table_index == seed.table_index
        and result.page_number == seed.page_number
    ):
        bonus += 18.0
    if intent == "feature_to_terminal" and result.entity_family == "register_or_control_item":
        bonus -= 90.0
    result_table_family = _infer_result_table_family(result)
    if result_table_family and result_table_family in planner_result.spec.table_family_preferences:
        bonus += 75.0
    if _result_matches_terms(result, planner_result.spec.section_hints):
        bonus += 35.0
    if _result_matches_terms(result, planner_result.spec.negative_terms):
        bonus -= 125.0
    if planner_result.spec.identifier_terms and _result_matches_terms(result, planner_result.spec.identifier_terms):
        bonus += 85.0
    if planner_result.spec.must_include_terms and _result_matches_terms(result, planner_result.spec.must_include_terms):
        bonus += 75.0
    if _looks_like_long_descriptive_entity(result):
        bonus -= 120.0
    return bonus


def _build_group_summary(
    *,
    seed: SearchResult,
    planner_result: QueryPlannerResult,
    feature_terms: list[str],
    item_count: int,
) -> str:
    """Build a compact summary for one evidence group."""

    if seed.table_index is not None:
        scope = f"structured table evidence on page {seed.page_number}"
    else:
        scope = f"page evidence on page {seed.page_number}"
    if feature_terms:
        return (
            f"{scope}; matched terms: {', '.join(feature_terms)}; "
            f"items: {item_count}; intent: {planner_result.spec.intent}; "
            f"planner_mode: {planner_result.mode}"
        )
    return f"{scope}; items: {item_count}; intent: {planner_result.spec.intent}; planner_mode: {planner_result.mode}"


def _serialize_query_item(result: SearchResult) -> dict[str, object]:
    """Serialize one search result for query evidence output."""

    return {
        "page_number": result.page_number,
        "chunk_type": result.chunk_type,
        "chunk_index": result.chunk_index,
        "table_index": result.table_index,
        "row_index": result.row_index,
        "row_type": result.row_type,
        "section_title": result.section_title,
        "table_title": result.table_title,
        "entity_family": result.entity_family,
        "entity_display_text": result.entity_display_text,
        "score": round(result.score, 4),
        "headers": result.headers,
        "cells": result.cells,
        "text": result.source_text,
    }


def _fetch_table_rows(
    *,
    connection: object,
    document_id: str,
    page_number: int,
    table_index: int,
) -> list[SearchResult]:
    """Load all rows from one logical stored table."""

    rows = connection.execute(
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
            table_rows.text_rendering,
            tables.crop_path
        FROM table_rows
        LEFT JOIN tables
          ON tables.document_id = table_rows.document_id
         AND tables.page_number = table_rows.page_number
         AND tables.table_index = table_rows.table_index
        WHERE table_rows.document_id = ?
          AND table_rows.page_number = ?
          AND table_rows.table_index = ?
        ORDER BY table_rows.row_index ASC
        """,
        (document_id, page_number, table_index),
    ).fetchall()
    results = []
    for row in rows:
        entity_family, entity_display_text = _lookup_row_entity_metadata(
            connection=connection,
            document_id=row["document_id"],
            page_number=row["page_number"],
            table_index=row["table_index"],
            row_index=row["row_index"],
        )
        results.append(
            SearchResult(
                document_id=row["document_id"],
                page_number=row["page_number"],
                chunk_index=row["row_index"],
                chunk_type=row["chunk_type"],
                source_text=row["text_rendering"],
                bm25_score=0.0,
                score=0.0,
                table_index=row["table_index"],
                row_index=row["row_index"],
                table_title=row["table_title"],
                section_title=row["section_title"],
                headers=json.loads(row["headers_json"]),
                cells=json.loads(row["cells_json"]),
                row_type=row["row_type"],
                crop_path=row["crop_path"],
                entity_family=entity_family,
                entity_display_text=entity_display_text,
            )
        )
    return results


def _fetch_page_chunks(
    *,
    connection: object,
    document_id: str,
    page_number: int,
) -> list[SearchResult]:
    """Load all stored text chunks from a page."""

    rows = connection.execute(
        """
        SELECT document_id, page_number, chunk_index, chunk_type, source_text
        FROM chunks
        WHERE document_id = ?
          AND page_number = ?
        ORDER BY chunk_index ASC
        """,
        (document_id, page_number),
    ).fetchall()
    return [
        SearchResult(
            document_id=row["document_id"],
            page_number=row["page_number"],
            chunk_index=row["chunk_index"],
            chunk_type=row["chunk_type"],
            source_text=row["source_text"],
            bm25_score=0.0,
            score=0.0,
        )
        for row in rows
    ]


def _lookup_row_entity_metadata(
    *,
    connection: object,
    document_id: str,
    page_number: int,
    table_index: int,
    row_index: int,
) -> tuple[str | None, str | None]:
    """Select the highest-value entity annotation for one stored table row."""

    rows = connection.execute(
        """
        SELECT entities.entity_family, entities.display_text
        FROM entity_evidence
        JOIN entities
          ON entities.document_id = entity_evidence.document_id
         AND entities.entity_key = entity_evidence.entity_key
        WHERE entity_evidence.document_id = ?
          AND entity_evidence.page_number = ?
          AND entity_evidence.table_index = ?
          AND entity_evidence.row_index = ?
          AND entity_evidence.source_kind = 'table_row'
        """,
        (document_id, page_number, table_index, row_index),
    ).fetchall()
    if not rows:
        return None, None
    priority = {
        "signal_or_terminal": 0,
        "component": 1,
        "register_or_control_item": 2,
        "spec_item": 3,
        "interface_or_feature": 4,
        "table_object": 5,
        "section_object": 6,
    }
    sorted_rows = sorted(rows, key=lambda item: priority.get(item["entity_family"], 99))
    return sorted_rows[0]["entity_family"], sorted_rows[0]["display_text"]


def _looks_like_terminal_headers(headers: list[str | None]) -> bool:
    """Detect pin/terminal style table headers."""

    normalized_headers = {
        "" if header is None else re.sub(r"\s+", " ", header).strip().lower()
        for header in headers
    }
    return bool(normalized_headers & {"pin", "ball", "pad", "terminal"}) and bool(
        normalized_headers & {"name", "function", "description", "signal"}
    )


def _infer_result_table_family(result: SearchResult) -> str | None:
    """Infer a coarse table family from one search result."""

    if result.headers:
        normalized_headers = {
            "" if header is None else re.sub(r"\s+", " ", header).strip().lower()
            for header in result.headers
        }
        if _looks_like_terminal_headers(result.headers):
            return "pin_table"
        if {"id", "r/w", "field", "value", "description"} & normalized_headers:
            return "register_table"
        if {"part number", "variant", "package", "ordering information", "order code"} & normalized_headers:
            return "variant_table"
        if {"parameter", "min", "typ", "max", "unit"} & normalized_headers:
            return "spec_table"
        return "generic_table"
    if result.entity_family == "signal_or_terminal":
        return "pin_table"
    if result.entity_family == "register_or_control_item":
        return "register_table"
    if result.entity_family == "component":
        return "variant_table"
    if result.entity_family == "spec_item":
        return "spec_table"
    return None


def _looks_like_long_descriptive_entity(result: SearchResult) -> bool:
    """Detect entity labels that are too sentence-like to act as strong seeds."""

    text = (result.entity_display_text or "").strip()
    if not text:
        return False
    words = text.split()
    if len(words) > 10:
        return True
    if len(text) > 72:
        return True
    if text.endswith("."):
        return True
    return False


def _result_matches_terms(result: SearchResult, terms: list[str]) -> bool:
    """Check whether a result matches any query expansion terms."""

    if not terms:
        return False
    haystack_parts = [result.source_text]
    if result.section_title:
        haystack_parts.append(result.section_title)
    if result.table_title:
        haystack_parts.append(result.table_title)
    if result.entity_display_text:
        haystack_parts.append(result.entity_display_text)
    if result.headers:
        haystack_parts.extend(header for header in result.headers if header)
    if result.cells:
        haystack_parts.extend(cell for cell in result.cells if cell)
    raw_haystack = " ".join(haystack_parts).lower()
    normalized_haystack = _normalize_text(" ".join(haystack_parts))
    for term in terms:
        lowered = term.lower()
        if lowered in raw_haystack:
            return True
        normalized_term = _normalize_text(term)
        if normalized_term and normalized_term in normalized_haystack:
            return True
    return False


def _prose_item_match_score(
    *,
    result: SearchResult,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> float:
    """Score one page-level prose chunk for conceptual evidence selection."""

    score = 0.0
    if result.chunk_type == "text_chunk":
        score += 20.0
    if _result_matches_terms(result, planner_result.spec.must_include_terms):
        score += 90.0
    if _result_matches_terms(result, feature_terms):
        score += 70.0
    elif _result_matches_terms(result, retrieval_tokens):
        score += 30.0
    if _result_matches_terms(result, planner_result.spec.section_hints):
        score += 45.0
    if _result_matches_terms(result, planner_result.spec.negative_terms):
        score -= 140.0
    if _looks_like_table_of_contents(result.source_text):
        score -= 260.0
    if _looks_like_dense_heading_listing(result.source_text):
        score -= 120.0
    if _looks_like_explanatory_prose(result.source_text):
        score += 120.0
    if _looks_like_heading_adjacent_body_text(result.source_text):
        score += 40.0
    return score


def _find_body_page_from_navigation_chunk(
    *,
    connection: object,
    document_id: str,
    navigation_text: str,
    planner_result: QueryPlannerResult,
    retrieval_tokens: list[str],
    feature_terms: list[str],
) -> int | None:
    """Use a TOC-like chunk as a navigation hint to locate a likely body page."""

    terms = [*planner_result.spec.must_include_terms, *feature_terms, *retrieval_tokens]
    normalized_navigation = _normalize_text(navigation_text)
    best_page: int | None = None
    best_score = 0.0
    page_rows = connection.execute(
        """
        SELECT page_number, source_text
        FROM chunks
        WHERE document_id = ?
          AND chunk_type = 'text_chunk'
        ORDER BY page_number ASC, chunk_index ASC
        """,
        (document_id,),
    ).fetchall()
    for row in page_rows:
        page_text = str(row["source_text"] or "")
        if _looks_like_table_of_contents(page_text):
            continue
        page_score = 0.0
        if _looks_like_explanatory_prose(page_text):
            page_score += 2.0
        if _looks_like_heading_adjacent_body_text(page_text):
            page_score += 1.0
        for term in terms:
            lowered = term.lower()
            normalized_term = _normalize_text(term)
            if lowered and lowered in page_text.lower():
                page_score += 1.5
            if normalized_term and normalized_term in _normalize_text(page_text):
                page_score += 1.0
            if lowered and lowered in normalized_navigation:
                page_score += 0.1
        if page_score > best_score:
            best_score = page_score
            best_page = int(row["page_number"])
    if best_score >= 2.5:
        return best_page
    return None


def _looks_like_dense_heading_listing(source_text: str) -> bool:
    """Detect chunks that mostly list section headings or index entries."""

    heading_hits = len(re.findall(r"\b\d+(?:\.\d+){1,3}\b", source_text))
    sentence_endings = len(re.findall(r"[.!?](?:\s|$)", source_text))
    if heading_hits >= 5 and sentence_endings <= 2:
        return True
    if source_text.count(" .") > 6:
        return True
    return False


def _looks_like_explanatory_prose(source_text: str) -> bool:
    """Detect sentence-like body prose that likely explains module behavior."""

    text = re.sub(r"\s+", " ", source_text).strip()
    if len(text) < 80:
        return False
    if _looks_like_table_of_contents(text):
        return False
    verb_hits = len(
        re.findall(
            r"\b(is|are|supports|uses|connects|provides|enables|allows|controls|configures|operates|works|transfers|maps)\b",
            text.lower(),
        )
    )
    sentence_endings = len(re.findall(r"[.!?](?:\s|$)", text))
    return verb_hits >= 1 and sentence_endings >= 1


def _looks_like_heading_adjacent_body_text(source_text: str) -> bool:
    """Detect body chunks that begin with a heading and then continue into prose."""

    text = re.sub(r"\s+", " ", source_text).strip()
    if re.match(r"^\d+(?:\.\d+){1,3}\s+[A-Z0-9][^.]{3,80}\.", text):
        return True
    if re.match(r"^[A-Z][A-Za-z0-9 /-]{4,80}\.\s+[A-Z]", text):
        return True
    return False


def _diverse_seed_selection(results: list[SearchResult], *, limit: int) -> list[SearchResult]:
    """Select top seeds while avoiding too many duplicates from one row or page."""

    selected: list[SearchResult] = []
    seen_rows: set[tuple[int, int | None, int | None, int]] = set()
    page_counts: dict[int, int] = {}
    for result in results:
        row_key = (result.page_number, result.table_index, result.row_index, result.chunk_index)
        if row_key in seen_rows:
            continue
        if page_counts.get(result.page_number, 0) >= 3:
            continue
        seen_rows.add(row_key)
        page_counts[result.page_number] = page_counts.get(result.page_number, 0) + 1
        selected.append(result)
        if len(selected) >= limit:
            break
    return selected


def _unique_preserving_order(values: list[str]) -> list[str]:
    """Deduplicate strings while keeping the original order."""

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    """Keep the best instance of each logical result."""

    deduped: dict[tuple[object, ...], SearchResult] = {}
    for result in results:
        key = (
            result.document_id,
            result.page_number,
            result.chunk_type,
            result.table_index,
            result.row_index,
            result.chunk_index,
            result.source_text,
        )
        existing = deduped.get(key)
        if existing is None or result.score > existing.score:
            deduped[key] = result
    return list(deduped.values())


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
