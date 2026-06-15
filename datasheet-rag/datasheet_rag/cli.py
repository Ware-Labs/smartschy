"""Typer CLI shell for datasheet-rag."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from datasheet_rag.answering import AnswerResult, generate_grounded_answer
from datasheet_rag.config import build_config
from datasheet_rag.logging_utils import configure_logging
from datasheet_rag.llm_provider import AnswerProviderConfigError, AnswerProviderError
from datasheet_rag.storage import (
    answer_query,
    evaluate_queries,
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


def _emit_answer_result(result: AnswerResult) -> None:
    """Render a grounded-answer result to stdout."""

    typer.echo(_format_answer_result(result), nl=False)


def _emit_evidence_response(response) -> None:
    """Render a structured evidence-pack response to stdout."""

    typer.echo(_format_evidence_response(response), nl=False)


def _emit_evidence_group_section(title: str, groups: list[dict[str, object]]) -> None:
    """Render one named evidence-group section."""

    typer.echo(_format_evidence_group_section(title, groups), nl=False)


def _format_answer_result(result: AnswerResult) -> str:
    """Build a stable plain-text rendering for grounded-answer output."""

    lines = [
        f"question: {result.question}",
        f"provider_mode: {result.provider_mode}",
        f"insufficient_evidence: {'yes' if result.insufficient_evidence else 'no'}",
        f"answer: {result.answer}",
        f"evidence_summary: {result.evidence_summary}",
    ]
    if result.sources:
        lines.append("sources:")
        for index, source in enumerate(result.sources, start=1):
            lines.append(f"{index}. page_number: {source.page_number}")
            if source.section_title:
                lines.append(f"   section_title: {source.section_title}")
            if source.table_title:
                lines.append(f"   table_title: {source.table_title}")
            if source.row_index is not None:
                lines.append(f"   row_index: {source.row_index}")
            lines.append(f"   chunk_type: {source.chunk_type}")
            if source.source_note:
                lines.append(f"   source_note: {source.source_note}")
    if result.uncertainty:
        lines.append(f"uncertainty: {result.uncertainty}")
    return "\n".join(lines) + "\n"


def _format_answer_with_evidence(result: AnswerResult, response) -> str:
    """Build the full answer package including answer output and evidence context."""

    return (
        f"{_format_answer_result(result)}"
        "evidence_context:\n"
        f"{_format_evidence_response(response)}"
    )


def _format_evidence_response(response) -> str:
    """Build a stable plain-text rendering for evidence-pack output."""

    lines = [
        f"question: {response.question}",
        f"planner_mode: {response.planner_mode}",
        f"rerank_mode: {response.rerank_mode}",
        f"intent: {response.intent}",
        f"primary_subject: {response.primary_subject}",
        f"must_include_terms: {json.dumps(response.must_include_terms, ensure_ascii=False)}",
        f"should_include_terms: {json.dumps(response.should_include_terms, ensure_ascii=False)}",
        f"identifier_terms: {json.dumps(response.identifier_terms, ensure_ascii=False)}",
        f"table_family_preferences: {json.dumps(response.table_family_preferences, ensure_ascii=False)}",
        f"preferred_evidence_families: {json.dumps(response.preferred_evidence_families, ensure_ascii=False)}",
        f"subquestions: {json.dumps(response.subquestions, ensure_ascii=False)}",
        f"section_hints: {json.dumps(response.section_hints, ensure_ascii=False)}",
        f"negative_terms: {json.dumps(response.negative_terms, ensure_ascii=False)}",
        f"candidate_family_summary: {json.dumps(response.candidate_family_summary, ensure_ascii=False)}",
        f"retrieval_summary: {response.retrieval_summary}",
    ]
    if response.coverage_notes:
        lines.append("coverage_notes:")
        lines.extend(f"- {note}" for note in response.coverage_notes)
    lines.append(_format_evidence_group_section("structured_evidence_groups", response.structured_evidence_groups).rstrip())
    lines.append(_format_evidence_group_section("prose_evidence_groups", response.prose_evidence_groups).rstrip())
    return "\n".join(lines) + "\n"


def _format_evidence_group_section(title: str, groups: list[dict[str, object]]) -> str:
    """Build a stable plain-text rendering for one evidence-group section."""

    lines = [f"{title}:"]
    if not groups:
        lines.append("- none")
        return "\n".join(lines) + "\n"
    for index, group in enumerate(groups, start=1):
        lines.append(f"{index}. page_number: {group['page_number']}")
        if group.get("table_index") is not None:
            lines.append(f"   table_index: {group['table_index']}")
        if group.get("section_title"):
            lines.append(f"   section_title: {group['section_title']}")
        if group.get("table_title"):
            lines.append(f"   table_title: {group['table_title']}")
        if group.get("evidence_family"):
            lines.append(f"   evidence_family: {group['evidence_family']}")
        if group.get("quality_score") is not None:
            lines.append(f"   quality_score: {group['quality_score']:.4f}")
        if group.get("rerank_reason"):
            lines.append(f"   rerank_reason: {group['rerank_reason']}")
        lines.append(f"   summary: {group['summary']}")
        lines.append(f"   group_score: {group['group_score']:.4f}")
        lines.append("   items:")
        for item in group["items"]:
            lines.append(f"   - chunk_type: {item['chunk_type']}")
            lines.append(f"     chunk_index: {item['chunk_index']}")
            if item.get("row_index") is not None:
                lines.append(f"     row_index: {item['row_index']}")
            if item.get("row_type") is not None:
                lines.append(f"     row_type: {item['row_type']}")
            if item.get("entity_family"):
                lines.append(f"     entity_family: {item['entity_family']}")
            if item.get("entity_display_text"):
                lines.append(f"     entity_display_text: {item['entity_display_text']}")
            lines.append(f"     score: {item['score']}")
            if item.get("headers") is not None and item.get("cells") is not None:
                lines.append(f"     headers: {json.dumps(item['headers'], ensure_ascii=False)}")
                lines.append(f"     cells: {json.dumps(item['cells'], ensure_ascii=False)}")
            lines.append(f"     text: {item['text']}")
    return "\n".join(lines) + "\n"


def _emit_or_save_output(*, text: str, output: Path | None) -> None:
    """Emit CLI text to stdout and optionally persist it to disk."""

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    typer.echo(_coerce_console_text(text), nl=False)
    if output is None:
        return
    typer.echo(_coerce_console_text(f"saved_output: {output}"))


def _coerce_console_text(text: str) -> str:
    """Make text safe for the current Windows console encoding."""

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _build_evidence_response(*, db: Path, log_level: str, question: str, limit: int):
    """Shared evidence-command implementation."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    return answer_query(db_path=config.db_path, question=question, limit=limit)


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
    workers: int | None = typer.Option(
        None,
        "--workers",
        min=1,
        help="Optional worker-process override. Defaults to automatic selection.",
    ),
) -> None:
    """Extract a PDF into SQLite documents/pages plus debug artifacts."""

    configure_logging(log_level)
    config = build_config(db_path=db, output_dir=out, log_level=log_level, workers=workers)
    result = ingest_pdf(
        pdf_path=pdf_path,
        db_path=config.db_path,
        output_dir=config.output_dir,
        force=force,
        workers=config.workers,
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
    typer.echo(f"worker_mode: {result.worker_mode}")
    typer.echo(f"selected_worker_count: {result.selected_worker_count}")
    typer.echo(f"probe_ran: {'yes' if result.probe_ran else 'no'}")
    typer.echo(f"entity_count: {result.entity_count}")
    typer.echo(f"entity_relation_count: {result.entity_relation_count}")


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
        if result.entity_family:
            typer.echo(f"   entity_family: {result.entity_family}")
        if result.entity_display_text:
            typer.echo(f"   entity_display_text: {result.entity_display_text}")
        typer.echo(f"   score: {result.score:.4f}")
        typer.echo(f"   bm25: {result.bm25_score:.4f}")
        if result.headers is not None and result.cells is not None:
            typer.echo(f"   headers: {json.dumps(result.headers, ensure_ascii=False)}")
            typer.echo(f"   cells: {json.dumps(result.cells, ensure_ascii=False)}")
        typer.echo(f"   text: {result.source_text}")


@app.command(name="evidence")
def evidence(
    question: str = typer.Argument(
        ...,
        help="Natural-language question used to assemble evidence for an external LLM.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to read from.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        help="Maximum number of evidence groups to display per channel.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Optional text file path to save the rendered evidence package.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Assemble a structured evidence pack for a natural-language question."""

    response = _build_evidence_response(db=db, log_level=log_level, question=question, limit=limit)
    _emit_or_save_output(text=_format_evidence_response(response), output=output)


@app.command(name="query", hidden=True)
def query_alias(
    question: str = typer.Argument(
        ...,
        help="Deprecated alias for `evidence`.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to read from.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        help="Maximum number of evidence groups to display per channel.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Optional text file path to save the rendered evidence package.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Deprecated alias for the `evidence` command."""

    response = _build_evidence_response(db=db, log_level=log_level, question=question, limit=limit)
    _emit_or_save_output(text=_format_evidence_response(response), output=output)


@app.command(name="answer")
def answer_command(
    question: str = typer.Argument(
        ...,
        help="Natural-language question answered only from retrieved evidence.",
    ),
    db: Path = typer.Option(
        Path("./datasheets.db"),
        "--db",
        help="SQLite database path to read from.",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        help="Maximum number of evidence groups to send into grounded answer generation per channel.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Optional text file path to save the rendered answer package.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Console log level.",
    ),
) -> None:
    """Generate a grounded answer using only the structured evidence pack."""

    response = _build_evidence_response(db=db, log_level=log_level, question=question, limit=limit)
    try:
        result = generate_grounded_answer(response)
    except AnswerProviderConfigError as exc:
        error_text = (
            f"answer_error: {exc}\n"
            "evidence_context:\n"
            f"{_format_evidence_response(response)}"
        )
        _emit_or_save_output(text=error_text, output=output)
        raise typer.Exit(code=1) from exc
    except AnswerProviderError as exc:
        error_text = (
            f"answer_error: {exc}\n"
            "evidence_context:\n"
            f"{_format_evidence_response(response)}"
        )
        _emit_or_save_output(text=error_text, output=output)
        raise typer.Exit(code=1) from exc
    _emit_or_save_output(text=_format_answer_with_evidence(result, response), output=output)


@app.command(name="eval")
def eval_command(
    eval_file: Path = typer.Argument(
        ...,
        exists=False,
        readable=True,
        resolve_path=True,
        help="Path to a JSON evaluation file.",
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
    """Run a compact grounded evaluation harness."""

    configure_logging(log_level)
    config = build_config(db_path=db, log_level=log_level)
    report = evaluate_queries(db_path=config.db_path, eval_file=eval_file)
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))


def main() -> None:
    """Console-script entry point."""

    app()
