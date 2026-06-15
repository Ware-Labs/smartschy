"""Typer CLI shell for datasheet-rag."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from datasheet_rag.config import build_config
from datasheet_rag.logging_utils import configure_logging
from datasheet_rag.phase_stubs import announce_stub
from datasheet_rag.storage import (
    format_document_summary,
    ingest_pdf,
    inspect_documents,
    search_records,
    serialize_document,
)

app = typer.Typer(
    help=(
        "Inspectable local CLI for ingesting technical PDFs into a retrieval-ready "
        "datasheet knowledge base."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def ingest(
    pdf_path: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        resolve_path=True,
        help="Path to the PDF to ingest with per-page text extraction.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path for canonical storage.",
    ),
    out: Path = typer.Option(
        Path("./out"),
        "--out",
        help="Directory for debug artifacts and extraction outputs.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-run extraction even if the same PDF hash is already stored with artifacts.",
    ),
) -> None:
    """Extract a PDF into SQLite documents/pages plus debug artifacts."""

    configure_logging(log_level)
    config = build_config(db_path=db, output_dir=out, log_level=log_level)
    result = ingest_pdf(
        pdf_path=pdf_path,
        db_path=config.db_path,
        output_dir=config.output_dir,
        force=force,
    )
    typer.echo(f"document_id: {result.document_id}")
    typer.echo(f"page_count: {result.page_count}")
    typer.echo(f"chunk_count: {result.chunk_count}")
    typer.echo(f"table_count: {result.table_count}")
    typer.echo(f"table_row_count: {result.table_row_count}")
    typer.echo(f"table_candidate_count: {result.table_candidate_count}")
    typer.echo(f"db_path: {result.db_path}")
    typer.echo(f"artifacts_dir: {result.output_dir}")
    typer.echo(f"reingested: {'yes' if result.replaced_existing else 'no'}")
    typer.echo(f"skipped: {'yes' if result.skipped_existing else 'no'}")


@app.command()
def inspect(
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to inspect.",
    ),
    doc: str | None = typer.Option(
        None,
        "--doc",
        help="Optional document identifier to narrow inspection output.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Inspect stored document metadata and page extraction output."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    documents = inspect_documents(db_path=config.db_path, document_id=doc)
    if doc is None:
        typer.echo(json.dumps([serialize_document(item) for item in documents], indent=2))
        return
    typer.echo(format_document_summary(documents[0]))


@app.command()
def search(
    query_text: str = typer.Argument(
        ...,
        help="Search query for lexical retrieval across prose chunks and table rows.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to search.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        help="Maximum number of results to display.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Search indexed prose chunks and extracted table rows."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    results = search_records(db_path=config.db_path, query_text=query_text, limit=limit)
    if not results:
        typer.echo("No retrieval matches found.")
        return

    for index, result in enumerate(results, start=1):
        typer.echo(f"{index}. document_id: {result.document_id}")
        typer.echo(f"   page_number: {result.page_number}")
        typer.echo(f"   chunk_index: {result.chunk_index}")
        typer.echo(f"   chunk_type: {result.chunk_type}")
        if result.table_index is not None:
            typer.echo(f"   table_index: {result.table_index}")
        if result.row_index is not None:
            typer.echo(f"   row_index: {result.row_index}")
        if result.row_type is not None:
            typer.echo(f"   row_type: {result.row_type}")
        if result.section_title:
            typer.echo(f"   section_title: {result.section_title}")
        if result.table_title:
            typer.echo(f"   table_title: {result.table_title}")
        if result.crop_path:
            typer.echo(f"   crop_path: {result.crop_path}")
        typer.echo(f"   score: {result.score:.4f}")
        typer.echo(f"   bm25: {result.bm25_score:.4f}")
        if result.headers is not None and result.cells is not None:
            typer.echo(f"   headers: {json.dumps(result.headers, ensure_ascii=False)}")
            typer.echo(f"   cells: {json.dumps(result.cells, ensure_ascii=False)}")
        typer.echo(f"   text: {result.source_text}")


@app.command()
def query(
    question: str = typer.Argument(
        ...,
        help="Natural-language question to answer from retrieved evidence.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to query.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Run the future retrieval-plus-LLM query flow."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    announce_stub(
        "query",
        [
            ("question", question),
            ("db", config.db_path),
            ("log_level", config.log_level),
        ],
    )


@app.command(name="eval")
def eval_command(
    eval_file: Path = typer.Argument(
        ...,
        exists=False,
        readable=True,
        resolve_path=True,
        help="Path to a YAML evaluation file.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path used during evaluation.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Run the future retrieval evaluation harness."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    announce_stub(
        "eval",
        [
            ("eval_file", eval_file),
            ("db", config.db_path),
            ("log_level", config.log_level),
        ],
    )


def main() -> None:
    """Console-script entry point."""

    app()
