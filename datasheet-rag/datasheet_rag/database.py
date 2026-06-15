"""SQLite storage helpers for canonical ingest records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite database and configure row access."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Create the canonical ingest schema if it does not already exist."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            file_hash TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            parser_name TEXT NOT NULL,
            page_count INTEGER NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            page_text TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            PRIMARY KEY (document_id, page_number),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_type TEXT NOT NULL,
            source_text TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            UNIQUE (document_id, page_number, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            source_text,
            content='chunks',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, source_text)
            VALUES (new.id, new.source_text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, source_text)
            VALUES ('delete', old.id, old.source_text);
        END;

        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, source_text)
            VALUES ('delete', old.id, old.source_text);
            INSERT INTO chunks_fts(rowid, source_text)
            VALUES (new.id, new.source_text);
        END;

        CREATE INDEX IF NOT EXISTS idx_chunks_document_page
            ON chunks(document_id, page_number, chunk_index);

        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            table_index INTEGER NOT NULL,
            table_title TEXT,
            section_title TEXT,
            headers_json TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            column_count INTEGER NOT NULL,
            bbox_json TEXT NOT NULL,
            detection_source TEXT NOT NULL DEFAULT 'pymupdf_find_tables',
            crop_path TEXT,
            visual_parser TEXT NOT NULL DEFAULT 'pillow-grid-native-words-v1',
            native_bbox_text TEXT NOT NULL DEFAULT '',
            confidence_json TEXT NOT NULL DEFAULT '{}',
            parser_family TEXT NOT NULL DEFAULT 'visual_fallback',
            parser_mode TEXT NOT NULL DEFAULT 'visual_fallback',
            table_kind TEXT NOT NULL DEFAULT 'visual_fallback_table',
            header_signature TEXT NOT NULL DEFAULT '',
            region_sources_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE (document_id, page_number, table_index),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS table_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            table_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            chunk_type TEXT NOT NULL,
            table_title TEXT,
            section_title TEXT,
            headers_json TEXT NOT NULL,
            cells_json TEXT NOT NULL,
            text_rendering TEXT NOT NULL,
            text_length INTEGER NOT NULL,
            row_type TEXT NOT NULL DEFAULT 'unknown',
            visual_cells_json TEXT NOT NULL DEFAULT '[]',
            native_fallback_text TEXT NOT NULL DEFAULT '',
            native_fallback_cells_json TEXT NOT NULL DEFAULT '[]',
            confidence_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (document_id, page_number, table_index, row_index),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS table_rows_fts USING fts5(
            text_rendering,
            content='table_rows',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS table_rows_ai AFTER INSERT ON table_rows BEGIN
            INSERT INTO table_rows_fts(rowid, text_rendering)
            VALUES (new.id, new.text_rendering);
        END;

        CREATE TRIGGER IF NOT EXISTS table_rows_ad AFTER DELETE ON table_rows BEGIN
            INSERT INTO table_rows_fts(table_rows_fts, rowid, text_rendering)
            VALUES ('delete', old.id, old.text_rendering);
        END;

        CREATE TRIGGER IF NOT EXISTS table_rows_au AFTER UPDATE ON table_rows BEGIN
            INSERT INTO table_rows_fts(table_rows_fts, rowid, text_rendering)
            VALUES ('delete', old.id, old.text_rendering);
            INSERT INTO table_rows_fts(rowid, text_rendering)
            VALUES (new.id, new.text_rendering);
        END;

        CREATE INDEX IF NOT EXISTS idx_tables_document_page
            ON tables(document_id, page_number, table_index);

        CREATE INDEX IF NOT EXISTS idx_table_rows_document_page
            ON table_rows(document_id, page_number, table_index, row_index);

        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            entity_family TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            display_text TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (document_id, entity_key),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS entity_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            table_index INTEGER,
            row_index INTEGER,
            chunk_index INTEGER,
            evidence_text TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS entity_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            source_entity_key TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            target_entity_key TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (document_id, source_entity_key, relation_type, target_entity_key),
            FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_entities_document_family
            ON entities(document_id, entity_family, normalized_key);

        CREATE INDEX IF NOT EXISTS idx_entity_evidence_document_page
            ON entity_evidence(document_id, page_number, table_index, row_index, chunk_index);

        CREATE INDEX IF NOT EXISTS idx_entity_relations_document
            ON entity_relations(document_id, source_entity_key, relation_type, target_entity_key);
        """
    )
    connection.execute(
        """
        INSERT INTO schema_metadata(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    _ensure_column(
        connection,
        "tables",
        "detection_source",
        "TEXT NOT NULL DEFAULT 'pymupdf_find_tables'",
    )
    _ensure_column(connection, "tables", "crop_path", "TEXT")
    _ensure_column(
        connection,
        "tables",
        "visual_parser",
        "TEXT NOT NULL DEFAULT 'pillow-grid-native-words-v1'",
    )
    _ensure_column(connection, "tables", "native_bbox_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "tables", "confidence_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "tables", "parser_family", "TEXT NOT NULL DEFAULT 'visual_fallback'")
    _ensure_column(connection, "tables", "parser_mode", "TEXT NOT NULL DEFAULT 'visual_fallback'")
    _ensure_column(connection, "tables", "table_kind", "TEXT NOT NULL DEFAULT 'visual_fallback_table'")
    _ensure_column(connection, "tables", "header_signature", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "tables", "region_sources_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "table_rows", "row_type", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(connection, "table_rows", "visual_cells_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "table_rows", "native_fallback_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        connection,
        "table_rows",
        "native_fallback_cells_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(connection, "table_rows", "confidence_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "entities", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(connection, "entities", "confidence", "REAL NOT NULL DEFAULT 0.0")
    _ensure_column(connection, "entities", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "entity_evidence", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(connection, "entity_relations", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    """Add a missing column to an existing table."""

    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in columns:
        return
    connection.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
    )
