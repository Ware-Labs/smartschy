from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from datasheet_rag.cli import app
from datasheet_rag.parallel_ingest import (
    build_probe_page_numbers,
    build_worker_candidates,
    compute_page_batches,
    heuristic_worker_ceiling,
)
from tests.test_cli import _create_sample_pdf, _parse_cli_kv

runner = CliRunner()


def test_heuristic_worker_ceiling_scales_with_page_count() -> None:
    assert heuristic_worker_ceiling(10, logical_cpu_count=16) == 1
    assert heuristic_worker_ceiling(160, logical_cpu_count=16) == 2
    assert heuristic_worker_ceiling(940, logical_cpu_count=16) == 11
    assert heuristic_worker_ceiling(4000, logical_cpu_count=8) == 8


def test_build_worker_candidates_stays_compact() -> None:
    assert build_worker_candidates(1) == [1]
    assert build_worker_candidates(2) == [1, 2]
    assert build_worker_candidates(11) == [1, 6, 11]


def test_compute_page_batches_covers_all_pages_in_order() -> None:
    page_numbers = list(range(1, 11))
    batches = compute_page_batches(page_numbers, 3)

    assert batches == [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        [9, 10],
    ]
    flattened = [page for batch in batches for page in batch]
    assert flattened == page_numbers


def test_build_probe_page_numbers_spreads_sample() -> None:
    sample = build_probe_page_numbers(300)

    assert sample[0] == 1
    assert len(sample) <= 48
    assert any(140 <= page <= 160 for page in sample)
    assert any(page >= 285 for page in sample)


def test_small_pdf_auto_mode_uses_one_worker(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    assert values["worker_mode"] == "auto"
    assert values["selected_worker_count"] == "1"
    assert values["probe_ran"] == "no"


def test_manual_worker_override_is_reported_and_skip_still_works(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    first = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir), "--workers", "2"],
    )
    second = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert first.exit_code == 0
    first_values = _parse_cli_kv(first.stdout)
    assert first_values["worker_mode"] == "manual"
    assert first_values["selected_worker_count"] == "2"

    assert second.exit_code == 0
    second_values = _parse_cli_kv(second.stdout)
    assert second_values["skipped"] == "yes"
