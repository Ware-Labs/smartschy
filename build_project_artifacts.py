#!/usr/bin/env python3
"""Unified project-centric artifact builder for DSN, BOM, and datasheets."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from bom_ingest import parse_bom_csv
from datasheet_linker import extract_datasheet_identifiers, link_components_to_datasheets
from datasheet_to_markdown import process_datasheet_pdf
from parse_dsn import normalize_dsn
from review_artifacts import ARTIFACT_FILE_ORDER, build_artifact_pack, write_artifact_pack


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _board_name_from_dsn(dsn_path: Path) -> str:
    stem = dsn_path.stem
    if stem.endswith(".normalized"):
        stem = stem[: -len(".normalized")]
    return stem.replace(" ", "_")


def _slugify(value: str) -> str:
    lowered = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return lowered or "datasheet"


def _discover_dsn(project_dir: Path) -> Path:
    dsn_candidates = sorted(project_dir.glob("*.dsn"))
    if not dsn_candidates:
        raise FileNotFoundError(f"No .dsn file found in project directory: {project_dir}")
    if len(dsn_candidates) > 1:
        raise ValueError(
            f"Multiple .dsn files found in {project_dir}; keep one .dsn per project folder or select explicitly."
        )
    return dsn_candidates[0]


def _discover_bom(project_dir: Path, dsn_path: Path) -> Path:
    preferred = project_dir / f"Bill of Materials-{dsn_path.stem}.csv"
    if preferred.exists():
        return preferred
    csv_candidates = sorted(project_dir.glob("*.csv"))
    if not csv_candidates:
        raise FileNotFoundError(f"No BOM .csv file found in project directory: {project_dir}")
    if len(csv_candidates) > 1:
        names = ", ".join(path.name for path in csv_candidates)
        raise ValueError(
            "Multiple BOM CSV files found and preferred filename is missing. "
            f"Expected {preferred.name} or a single CSV. Found: {names}"
        )
    return csv_candidates[0]


def _discover_datasheets(project_dir: Path) -> List[Path]:
    resources_dir = project_dir / "resources"
    if not resources_dir.exists():
        return []
    return sorted(resources_dir.glob("*.pdf"))


def _relative_to(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _build_linking_report(
    project_name: str,
    link_manifest: Dict[str, Any],
) -> str:
    summary = link_manifest.get("summary", {})
    lines = [
        f"# Datasheet Linking Report: {project_name}",
        "",
        "## Summary",
        f"- Components reviewed: {summary.get('component_count', 0)}",
        f"- Linked (exact): {summary.get('linked_exact', 0)}",
        f"- Linked (heuristic): {summary.get('linked_heuristic', 0)}",
        f"- Ambiguous: {summary.get('ambiguous', 0)}",
        f"- Unmatched: {summary.get('unmatched', 0)}",
        f"- Datasheets linked: {summary.get('linked_datasheet_count', 0)} / {summary.get('datasheet_count', 0)}",
        "",
        "## Unmatched Components",
    ]
    unmatched = [row for row in link_manifest.get("components", []) if row.get("status") == "unmatched"]
    if unmatched:
        for row in unmatched:
            lines.append(f"- {row.get('ref')}: {row.get('part_number')} ({row.get('reason')})")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Ambiguous Components")
    ambiguous = [row for row in link_manifest.get("components", []) if row.get("status") == "ambiguous"]
    if ambiguous:
        for row in ambiguous:
            candidate_ids = [str(c.get("datasheet_id")) for c in row.get("candidates", [])]
            lines.append(f"- {row.get('ref')}: {row.get('part_number')} -> {', '.join(candidate_ids)}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Unlinked Datasheets")
    unlinked = [row for row in link_manifest.get("datasheets", []) if row.get("link_status") == "unlinked"]
    if unlinked:
        for row in unlinked:
            lines.append(f"- {row.get('datasheet_id')}: {row.get('source_pdf')}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build unified project artifacts from DSN, BOM, and datasheets.")
    parser.add_argument("--project-dir", required=True, help="Project folder containing .dsn, BOM CSV, and resources/*.pdf")
    parser.add_argument("--out-root", default="derived", help="Root output directory (default: derived)")
    parser.add_argument("--use-llm", action="store_true", help="Enable optional LLM cleanup stage for datasheet markdown.")
    parser.add_argument("--mock-llm", action="store_true", help="Use deterministic mock cleanup instead of OpenAI.")
    parser.add_argument("--model", default="gpt-4.1-mini", help="Model for optional --use-llm cleanup.")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR fallback for low-text pages.")
    parser.add_argument(
        "--ocr-char-threshold",
        type=int,
        default=40,
        help="OCR candidate threshold for extracted characters per page.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print progress output.")
    return parser.parse_args()


def build_project_artifacts(args: argparse.Namespace) -> Dict[str, Any]:
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory does not exist: {project_dir}")

    dsn_path = _discover_dsn(project_dir)
    bom_path = _discover_bom(project_dir, dsn_path)
    datasheet_pdfs = _discover_datasheets(project_dir)
    board_name = _board_name_from_dsn(dsn_path)

    out_root = Path(args.out_root).resolve()
    project_out = out_root / board_name
    datasheets_out = project_out / "datasheets"
    project_out.mkdir(parents=True, exist_ok=True)
    datasheets_out.mkdir(parents=True, exist_ok=True)

    if args.verbose:
        print(f"[project] DSN: {dsn_path}")
        print(f"[project] BOM: {bom_path}")
        print(f"[project] Datasheets discovered: {len(datasheet_pdfs)}")
        print(f"[project] Output: {project_out}")

    # Existing board-level DSN/BOM artifacts.
    bom_model = parse_bom_csv(bom_path)
    normalized_dsn = normalize_dsn(dsn_path)
    pack = build_artifact_pack(normalized_dsn, bom_model)
    write_artifact_pack(pack, project_out)

    datasheet_rows: List[Dict[str, Any]] = []
    slug_count: Dict[str, int] = {}
    for pdf_path in datasheet_pdfs:
        base_slug = _slugify(pdf_path.stem)
        slug_count[base_slug] = slug_count.get(base_slug, 0) + 1
        suffix = slug_count[base_slug]
        datasheet_id = f"{base_slug}-{suffix:02d}" if suffix > 1 else base_slug
        datasheet_dir = datasheets_out / datasheet_id
        try:
            result = process_datasheet_pdf(
                pdf_path,
                datasheet_dir,
                use_llm=bool(args.use_llm),
                mock_llm=bool(args.mock_llm),
                model=str(args.model),
                ocr=bool(args.ocr),
                ocr_char_threshold=int(args.ocr_char_threshold),
                verbose=bool(args.verbose),
            )
            identifier_info = extract_datasheet_identifiers(result["normalized"], pdf_path)
            datasheet_rows.append(
                {
                    "datasheet_id": datasheet_id,
                    "source_pdf": str(pdf_path),
                    "output_dir": str(datasheet_dir),
                    "datasheet_markdown": str(datasheet_dir / "datasheet.md"),
                    "normalized_document": str(datasheet_dir / "normalized_document.json"),
                    "raw_extraction": str(datasheet_dir / "raw_extraction.json"),
                    "extraction_report": str(datasheet_dir / "extraction_report.json"),
                    "status": "ok",
                    "error": None,
                    **identifier_info,
                }
            )
        except Exception as exc:
            datasheet_rows.append(
                {
                    "datasheet_id": datasheet_id,
                    "source_pdf": str(pdf_path),
                    "output_dir": str(datasheet_dir),
                    "datasheet_markdown": str(datasheet_dir / "datasheet.md"),
                    "normalized_document": str(datasheet_dir / "normalized_document.json"),
                    "raw_extraction": str(datasheet_dir / "raw_extraction.json"),
                    "extraction_report": str(datasheet_dir / "extraction_report.json"),
                    "status": "failed",
                    "error": str(exc),
                    "identifier_tokens": [],
                    "identifier_norms": [],
                    "search_blob_norm": "",
                    "first_page_excerpt": "",
                }
            )

    successful_datasheets = [row for row in datasheet_rows if row.get("status") == "ok"]
    link_manifest = link_components_to_datasheets(pack["bom_crosswalk"], successful_datasheets)
    datasheet_state_by_id = {str(row.get("datasheet_id")): row for row in datasheet_rows}
    for row in link_manifest.get("datasheets", []):
        base_row = datasheet_state_by_id.get(str(row.get("datasheet_id")))
        if not base_row:
            continue
        base_row["linked_refs"] = row.get("linked_refs", [])
        base_row["link_status"] = row.get("link_status", "unlinked")
    for row in datasheet_rows:
        row.setdefault("linked_refs", [])
        row.setdefault("link_status", "failed" if row.get("status") == "failed" else "unlinked")

    datasheet_manifest = {
        "schema_version": "1.0.0",
        "project": board_name,
        "summary": {
            "datasheet_count": len(datasheet_rows),
            "successful_count": sum(1 for row in datasheet_rows if row.get("status") == "ok"),
            "failed_count": sum(1 for row in datasheet_rows if row.get("status") == "failed"),
            "linked_count": sum(1 for row in datasheet_rows if row.get("link_status") == "linked"),
            "unlinked_count": sum(1 for row in datasheet_rows if row.get("link_status") == "unlinked"),
        },
        "datasheets": datasheet_rows,
    }

    component_links = {
        "schema_version": "1.0.0",
        "project": board_name,
        "summary": link_manifest.get("summary", {}),
        "components": link_manifest.get("components", []),
    }

    linking_report_path = project_out / "linking_report.md"
    linking_report_path.write_text(_build_linking_report(board_name, link_manifest), encoding="utf-8")

    project_manifest = {
        "schema_version": "1.0.0",
        "project": board_name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "project_dir": str(project_dir),
            "dsn": str(dsn_path),
            "bom_csv": str(bom_path),
            "resources_dir": str(project_dir / "resources"),
            "datasheet_pdfs": [str(path) for path in datasheet_pdfs],
        },
        "outputs": {
            "project_dir": str(project_out),
            "board_artifacts": {key: str(project_out / name) for name, key in ARTIFACT_FILE_ORDER},
            "datasheet_manifest": str(project_out / "datasheet_manifest.json"),
            "component_datasheet_links": str(project_out / "component_datasheet_links.json"),
            "linking_report": str(linking_report_path),
            "qa_context": str(project_out / "qa_context.json"),
        },
        "run_config": {
            "use_llm": bool(args.use_llm),
            "mock_llm": bool(args.mock_llm),
            "model": str(args.model),
            "ocr": bool(args.ocr),
            "ocr_char_threshold": int(args.ocr_char_threshold),
        },
    }

    qa_context = {
        "schema_version": "1.0.0",
        "project": board_name,
        "question_workflow_inputs": {
            "board_artifacts_dir": str(project_out),
            "datasheet_manifest": str(project_out / "datasheet_manifest.json"),
            "component_datasheet_links": str(project_out / "component_datasheet_links.json"),
            "linking_report": str(linking_report_path),
        },
        "board_artifacts": {key: str(project_out / name) for name, key in ARTIFACT_FILE_ORDER},
        "components": component_links["components"],
        "datasheets": [
            {
                "datasheet_id": row.get("datasheet_id"),
                "status": row.get("status"),
                "link_status": row.get("link_status"),
                "linked_refs": row.get("linked_refs", []),
                "source_pdf": row.get("source_pdf"),
                "datasheet_markdown": row.get("datasheet_markdown"),
            }
            for row in datasheet_rows
        ],
    }

    _write_json(project_out / "project_manifest.json", project_manifest)
    _write_json(project_out / "datasheet_manifest.json", datasheet_manifest)
    _write_json(project_out / "component_datasheet_links.json", component_links)
    _write_json(project_out / "qa_context.json", qa_context)

    if args.verbose:
        print(f"[project] Wrote project manifest: {project_out / 'project_manifest.json'}")
        print(f"[project] Wrote datasheet manifest: {project_out / 'datasheet_manifest.json'}")
        print(f"[project] Wrote component links: {project_out / 'component_datasheet_links.json'}")
        print(f"[project] Wrote QA context: {project_out / 'qa_context.json'}")
        print(f"[project] Wrote linking report: {linking_report_path}")

    return {
        "project_dir": str(project_out),
        "project_manifest": _relative_to(project_out / "project_manifest.json", out_root),
        "datasheet_manifest": _relative_to(project_out / "datasheet_manifest.json", out_root),
        "component_links": _relative_to(project_out / "component_datasheet_links.json", out_root),
        "qa_context": _relative_to(project_out / "qa_context.json", out_root),
        "linking_report": _relative_to(linking_report_path, out_root),
    }


def main() -> None:
    args = parse_args()
    build_project_artifacts(args)


if __name__ == "__main__":
    main()
