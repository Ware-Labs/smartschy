from __future__ import annotations

from pathlib import Path

from .datasheet_facts import DatasheetFactsOptions, extract_component_facts
from .datasheet_markdown import build_datasheet_markdown
from .bom_index import build_bom_indices
from .dsn_index import build_dsn_indices
from .model_policy import ANSWER_MODEL_DEFAULT
from .pdf_ingest import build_pdf_chunks
from .semantic_index import build_semantic_indices
from .utils import write_json


def _validate_inputs(
    dsn_path: Path,
    bom_csv_path: Path,
    schematic_pdf: Path,
    resources_dir: Path,
) -> None:
    if not dsn_path.exists():
        raise FileNotFoundError(f"Missing DSN file: {dsn_path}")
    if not bom_csv_path.exists():
        raise FileNotFoundError(f"Missing BOM CSV: {bom_csv_path}")
    if not schematic_pdf.exists():
        raise FileNotFoundError(f"Missing schematic PDF: {schematic_pdf}")
    if not resources_dir.exists():
        raise FileNotFoundError(f"Missing datasheet directory: {resources_dir}")


def ingest_project_with_inputs(
    *,
    project_root: Path,
    dsn_path: Path,
    bom_csv_path: Path,
    schematic_pdf: Path,
    resources_dir: Path,
    llm_enrich: bool = False,
    llm_model: str = "gpt-5-mini",
) -> dict[str, object]:
    _validate_inputs(
        dsn_path=dsn_path,
        bom_csv_path=bom_csv_path,
        schematic_pdf=schematic_pdf,
        resources_dir=resources_dir,
    )

    derived_dir = project_root / "derived"
    dsn_dir = derived_dir / "dsn"
    bom_dir = derived_dir / "bom"
    pdf_dir = derived_dir / "pdf"
    datasheet_dir = derived_dir / "datasheets"

    dsn_stats = build_dsn_indices(dsn_path, dsn_dir)
    bom_stats = build_bom_indices(bom_csv_path, resources_dir, bom_dir)
    pdf_stats = build_pdf_chunks(schematic_pdf, resources_dir, pdf_dir)
    datasheet_markdown_stats = build_datasheet_markdown(resources_dir, datasheet_dir)
    datasheet_fact_stats = extract_component_facts(
        project_root=project_root,
        options=DatasheetFactsOptions(
            llm_model=llm_model if llm_enrich else ANSWER_MODEL_DEFAULT,
            overlap=0.25,
            early_stop=True,
        ),
    )
    semantic_stats = build_semantic_indices(
        project_root=project_root,
        llm_enrich=llm_enrich,
        llm_model=llm_model,
    )

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
            "datasheet_dir": str(datasheet_dir),
            "bom_markdown": str(bom_dir / "bom_overview.md"),
            "circuit_summary_markdown": str(derived_dir / "kg" / "circuit_summary.md"),
        },
        "stats": {
            "dsn": dsn_stats,
            "bom": bom_stats,
            "pdf": pdf_stats,
            "datasheet_markdown": datasheet_markdown_stats,
            "datasheet_facts": datasheet_fact_stats,
            "semantic": semantic_stats,
        },
        "llm_enrich": llm_enrich,
        "llm_model": llm_model if llm_enrich else "",
    }
    write_json(derived_dir / "ingest_summary.json", summary)
    return summary


def ingest_project(
    project_root: Path,
    llm_enrich: bool = False,
    llm_model: str = "gpt-5-mini",
) -> dict[str, object]:
    keen_root = project_root / "keen3_filet"
    dsn_path = keen_root / "keen3_filet.dsn"
    bom_csv_path = keen_root / "Bill of Materials-keen3_filet.csv"
    schematic_pdf = keen_root / "keen3_filet_2026-05-04.pdf"
    resources_dir = keen_root / "resources"

    return ingest_project_with_inputs(
        project_root=project_root,
        dsn_path=dsn_path,
        bom_csv_path=bom_csv_path,
        schematic_pdf=schematic_pdf,
        resources_dir=resources_dir,
        llm_enrich=llm_enrich,
        llm_model=llm_model,
    )
