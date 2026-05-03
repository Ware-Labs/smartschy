#!/usr/bin/env python3
"""BOM CSV ingestion and DSN crosswalk helpers."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


EXPECTED_HEADERS = [
    "Designator",
    "Quantity",
    "Value",
    "Manufacturer",
    "Part Number",
    "Note",
    "Specification",
    "Footprint",
]
REF_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _normalize_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_ref_token(token: str) -> Optional[str]:
    token = _normalize_text(token).strip(",")
    if not token:
        return None
    token = token.upper()
    if not REF_TOKEN_RE.match(token):
        return None
    return token


def _explode_designators(value: str) -> Dict[str, List[str]]:
    refs: List[str] = []
    unparsed: List[str] = []
    raw_tokens = [t.strip() for t in value.split(",")]
    for token in raw_tokens:
        if not token:
            continue
        norm = _normalize_ref_token(token)
        if norm is None:
            unparsed.append(token)
            continue
        refs.append(norm)
    return {"refs": refs, "unparsed_tokens": unparsed}


def parse_bom_csv(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    headers = list(reader.fieldnames or [])
    missing_headers = [h for h in EXPECTED_HEADERS if h not in headers]
    if missing_headers:
        raise ValueError(
            f"{path}: missing required BOM headers: {', '.join(missing_headers)}"
        )

    rows: List[Dict[str, Any]] = []
    ref_to_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    duplicate_refs: Dict[str, List[str]] = defaultdict(list)
    quantity_mismatches: List[Dict[str, Any]] = []
    unparsed_designator_tokens: List[Dict[str, Any]] = []

    for row_index, row in enumerate(reader, start=1):
        if not any((row or {}).values()):
            continue
        normalized_row = {
            "row_id": f"BOM_ROW_{row_index}",
            "source_line": row_index + 1,  # account for CSV header line
            "name": _normalize_text(row.get("Value", "")),
            "description": _normalize_text(row.get("Specification", "")),
            "designator_raw": _normalize_text(row.get("Designator", "")),
            "footprint": _normalize_text(row.get("Footprint", "")),
            "libref": _normalize_text(row.get("Part Number", "")),
            "quantity_raw": _normalize_text(row.get("Quantity", "")),
            "manufacturer": _normalize_text(row.get("Manufacturer", "")),
            "part_number": _normalize_text(row.get("Part Number", "")),
            "note": _normalize_text(row.get("Note", "")),
            "specification": _normalize_text(row.get("Specification", "")),
            "raw": {k: _normalize_text(v) for k, v in row.items()},
        }
        exploded = _explode_designators(normalized_row["designator_raw"])
        normalized_row["refs"] = sorted(exploded["refs"])
        normalized_row["unparsed_designator_tokens"] = exploded["unparsed_tokens"]
        try:
            normalized_row["quantity"] = int(normalized_row["quantity_raw"])
        except ValueError:
            normalized_row["quantity"] = None

        if normalized_row["quantity"] is not None and normalized_row["quantity"] != len(normalized_row["refs"]):
            quantity_mismatches.append(
                {
                    "row_id": normalized_row["row_id"],
                    "quantity": normalized_row["quantity"],
                    "exploded_ref_count": len(normalized_row["refs"]),
                    "designator_raw": normalized_row["designator_raw"],
                }
            )

        for bad in normalized_row["unparsed_designator_tokens"]:
            unparsed_designator_tokens.append(
                {
                    "row_id": normalized_row["row_id"],
                    "token": bad,
                    "designator_raw": normalized_row["designator_raw"],
                }
            )

        rows.append(normalized_row)

    for row in rows:
        for ref in row["refs"]:
            ref_to_rows[ref].append(row)

    for ref, ref_rows in ref_to_rows.items():
        if len(ref_rows) > 1:
            duplicate_refs[ref] = [r["row_id"] for r in ref_rows]

    resolved_columns = {
        "designator": "Designator",
        "quantity": "Quantity",
        "name": "Value",
        "manufacturer": "Manufacturer",
        "part_number": "Part Number",
        "note": "Note",
        "specification": "Specification",
        "footprint": "Footprint",
    }

    return {
        "schema_version": "1.0.0",
        "source": {"path": str(path), "format": "csv_bom"},
        "headers": headers,
        "expected_headers": EXPECTED_HEADERS,
        "resolved_columns": resolved_columns,
        "rows": rows,
        "indexes": {
            "ref_to_rows": {
                ref: [r["row_id"] for r in ref_rows]
                for ref, ref_rows in sorted(ref_to_rows.items())
            }
        },
        "diagnostics": {
            "header_mismatch": sorted(set(EXPECTED_HEADERS) - set(headers)),
            "required_columns_missing": [],
            "duplicate_bom_refs": dict(sorted(duplicate_refs.items())),
            "quantity_mismatches": quantity_mismatches,
            "unparsed_designator_tokens": unparsed_designator_tokens,
        },
    }


def classify_component(ref: str, bom_name: str = "") -> str:
    up_ref = ref.upper()
    up_name = bom_name.upper()
    if up_ref.startswith("TP"):
        return "testpoint"
    if up_ref.startswith("J") or up_ref.startswith("P"):
        return "connector"
    if up_ref.startswith("SW"):
        return "switch"
    if up_ref.startswith("R"):
        return "resistor"
    if up_ref.startswith("C"):
        return "capacitor"
    if up_ref.startswith("L") or up_ref.startswith("FB"):
        return "inductor_or_bead"
    if up_ref.startswith("D"):
        return "diode_or_led"
    if up_ref.startswith("U") or up_ref.startswith("MOD"):
        return "ic_or_module"
    if "CONNECTOR" in up_name:
        return "connector"
    return "other"
