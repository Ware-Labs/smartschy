from __future__ import annotations

from pathlib import Path

from .bom_index import build_bom_indices
from .dsn_index import build_dsn_indices
from .pdf_ingest import build_pdf_chunks
from .utils import write_json


def ingest_project(project_root: Path) -> dict[str, object]:
    keen_root = project_root / "keen3_filet"
    dsn_path = keen_root / "keen3_filet.dsn"
    bom_csv_path = keen_root / "Bill of Materials-keen3_filet.csv"
    schematic_pdf = keen_root / "keen3_filet_2026-05-04.pdf"
    resources_dir = keen_root / "resources"

    if not dsn_path.exists():
        raise FileNotFoundError(f"Missing DSN file: {dsn_path}")
    if not bom_csv_path.exists():
        raise FileNotFoundError(f"Missing BOM CSV: {bom_csv_path}")
    if not schematic_pdf.exists():
        raise FileNotFoundError(f"Missing schematic PDF: {schematic_pdf}")
    if not resources_dir.exists():
        raise FileNotFoundError(f"Missing datasheet directory: {resources_dir}")

    derived_dir = project_root / "derived"
    dsn_dir = derived_dir / "dsn"
    bom_dir = derived_dir / "bom"
    pdf_dir = derived_dir / "pdf"

    dsn_stats = build_dsn_indices(dsn_path, dsn_dir)
    bom_stats = build_bom_indices(bom_csv_path, resources_dir, bom_dir)
    pdf_stats = build_pdf_chunks(schematic_pdf, resources_dir, pdf_dir)

    summary = {
        "project_root": str(project_root),
        "inputs": {
            "dsn": str(dsn_path),
            "bom": str(bom_csv_path),
            "schematic_pdf": str(schematic_pdf),
            "resources_dir": str(resources_dir),
        },
        "outputs": {
            "dsn_dir": str(dsn_dir),
            "bom_dir": str(bom_dir),
            "pdf_dir": str(pdf_dir),
        },
        "stats": {
            "dsn": dsn_stats,
            "bom": bom_stats,
            "pdf": pdf_stats,
        },
    }
    write_json(derived_dir / "ingest_summary.json", summary)
    return summary
