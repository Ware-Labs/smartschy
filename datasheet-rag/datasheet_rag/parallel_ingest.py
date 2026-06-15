"""Parallel table extraction scheduling for PDF ingest."""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

import fitz

from datasheet_rag.table_extraction import (
    ExtractedTable,
    ExtractedTableRow,
    TableExtractionResult,
    TextLine,
    extract_tables_from_pages,
)

MIN_PAGES_PER_WORKER = 80
MIN_PAGES_FOR_PROBE = 96
PROBE_SAMPLE_PAGES = 48


@dataclass(slots=True)
class WorkerSelection:
    """Resolved ingest worker settings and probe metadata."""

    worker_mode: str
    selected_worker_count: int
    probe_ran: bool
    probe_candidates: list[int]
    probe_winner: int | None
    batch_count: int
    batch_size: int


def heuristic_worker_ceiling(
    page_count: int,
    logical_cpu_count: int | None = None,
) -> int:
    """Compute a conservative automatic worker ceiling."""

    logical = max(1, logical_cpu_count or os.cpu_count() or 1)
    if page_count <= 0:
        return 1
    max_workers_from_pages = max(1, page_count // MIN_PAGES_PER_WORKER)
    return max(1, min(logical, max_workers_from_pages))


def build_worker_candidates(worker_ceiling: int) -> list[int]:
    """Build a compact candidate set for tiny startup probing."""

    ceiling = max(1, worker_ceiling)
    if ceiling == 1:
        return [1]
    if ceiling == 2:
        return [1, 2]
    midpoint = max(2, int(round(ceiling / 2)))
    return sorted({1, midpoint, ceiling})


def should_run_probe(
    *,
    page_count: int,
    worker_ceiling: int,
    manual_workers: int | None,
) -> bool:
    """Return whether auto mode should run the tiny startup probe."""

    if manual_workers is not None:
        return False
    if worker_ceiling <= 1:
        return False
    return page_count >= MIN_PAGES_FOR_PROBE


def build_probe_page_numbers(
    page_count: int,
    *,
    sample_pages: int = PROBE_SAMPLE_PAGES,
) -> list[int]:
    """Pick a small deterministic sample spread across the document."""

    if page_count <= sample_pages:
        return list(range(1, page_count + 1))

    window_count = 3
    window_size = max(1, sample_pages // window_count)
    windows: list[tuple[int, int]] = []
    starts = [
        1,
        max(1, (page_count // 2) - (window_size // 2)),
        max(1, page_count - window_size + 1),
    ]
    for start in starts:
        end = min(page_count, start + window_size - 1)
        windows.append((start, end))

    sample: list[int] = []
    for start, end in windows:
        sample.extend(range(start, end + 1))
    return sorted({page for page in sample if 1 <= page <= page_count})


def compute_page_batches(
    page_numbers: list[int],
    worker_count: int,
) -> list[list[int]]:
    """Split page numbers into stable, contiguous batches."""

    if not page_numbers:
        return []
    worker_count = max(1, worker_count)
    batch_count = min(worker_count, len(page_numbers))
    batch_size = math.ceil(len(page_numbers) / batch_count)
    return [
        page_numbers[index: index + batch_size]
        for index in range(0, len(page_numbers), batch_size)
    ]


def select_worker_count(
    *,
    pdf_path: Path,
    page_count: int,
    page_lines: dict[int, list[TextLine]],
    manual_workers: int | None,
) -> WorkerSelection:
    """Choose the effective worker count using auto mode or override."""

    if manual_workers is not None:
        selected = max(1, manual_workers)
        return WorkerSelection(
            worker_mode="manual",
            selected_worker_count=selected,
            probe_ran=False,
            probe_candidates=[selected],
            probe_winner=None,
            batch_count=min(selected, page_count) if page_count else 0,
            batch_size=math.ceil(page_count / min(selected, page_count)) if page_count else 0,
        )

    worker_ceiling = heuristic_worker_ceiling(page_count)
    candidates = build_worker_candidates(worker_ceiling)
    selected = worker_ceiling
    probe_ran = should_run_probe(
        page_count=page_count,
        worker_ceiling=worker_ceiling,
        manual_workers=manual_workers,
    )
    probe_winner = None

    if probe_ran:
        sample_pages = build_probe_page_numbers(page_count)
        benchmark_scores: dict[int, float] = {}
        for candidate in candidates:
            started = perf_counter()
            _ = extract_tables_parallel(
                pdf_path=pdf_path,
                page_numbers=sample_pages,
                page_lines=page_lines,
                worker_count=candidate,
                crop_dir=None,
            )
            elapsed = perf_counter() - started
            benchmark_scores[candidate] = elapsed / max(len(sample_pages), 1)
        selected = min(
            candidates,
            key=lambda candidate: (benchmark_scores[candidate], candidate),
        )
        probe_winner = selected

    batches = compute_page_batches(list(range(1, page_count + 1)), selected)
    batch_size = max((len(batch) for batch in batches), default=0)
    return WorkerSelection(
        worker_mode="auto",
        selected_worker_count=selected,
        probe_ran=probe_ran,
        probe_candidates=candidates,
        probe_winner=probe_winner,
        batch_count=len(batches),
        batch_size=batch_size,
    )


def extract_tables_parallel(
    *,
    pdf_path: Path,
    page_numbers: list[int],
    page_lines: dict[int, list[TextLine]],
    worker_count: int,
    crop_dir: Path | None,
) -> TableExtractionResult:
    """Extract table results across batches with independent worker processes."""

    if not page_numbers:
        return TableExtractionResult(tables=[], rows=[], candidate_count=0)
    if worker_count <= 1:
        return _worker_extract_pdf_pages(
            pdf_path=pdf_path,
            page_numbers=page_numbers,
            page_lines=page_lines,
            crop_dir=crop_dir,
        )

    batches = compute_page_batches(page_numbers, worker_count)
    tables: list[ExtractedTable] = []
    rows: list[ExtractedTableRow] = []
    candidate_count = 0
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                _worker_extract_pdf_pages,
                pdf_path=pdf_path,
                page_numbers=batch,
                page_lines=page_lines,
                crop_dir=crop_dir,
            )
            for batch in batches
        ]
        results = [future.result() for future in futures]

    for result in sorted(
        results,
        key=lambda item: (
            item.tables[0].page_number if item.tables else item.rows[0].page_number if item.rows else 10**9
        ),
    ):
        tables.extend(result.tables)
        rows.extend(result.rows)
        candidate_count += result.candidate_count

    tables.sort(key=lambda item: (item.page_number, item.table_index))
    rows.sort(key=lambda item: (item.page_number, item.table_index, item.row_index))
    return TableExtractionResult(tables=tables, rows=rows, candidate_count=candidate_count)


def _worker_extract_pdf_pages(
    *,
    pdf_path: Path,
    page_numbers: list[int],
    page_lines: dict[int, list[TextLine]],
    crop_dir: Path | None,
) -> TableExtractionResult:
    """Worker entrypoint that opens the PDF and extracts one page batch."""

    with fitz.open(pdf_path) as document:
        return extract_tables_from_pages(
            document=document,
            page_numbers=page_numbers,
            page_lines=page_lines,
            crop_dir=crop_dir,
        )


def serialize_worker_selection(selection: WorkerSelection) -> dict[str, object]:
    """Expose worker selection metadata as JSON-compatible data."""

    return asdict(selection)
