#!/usr/bin/env python3
"""Link BOM components to extracted datasheet artifacts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{2,}")


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "").upper())


def _looks_like_part_number(token: str) -> bool:
    up = str(token).upper()
    if len(up) < 4:
        return False
    return any(ch.isalpha() for ch in up) and any(ch.isdigit() for ch in up)


def _candidate_tokens(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for token in TOKEN_RE.findall(text or ""):
        if token in seen:
            continue
        seen.add(token)
        if _looks_like_part_number(token):
            out.append(token)
    return out


def _first_page_text(normalized_document: Dict[str, Any]) -> str:
    pages = list(normalized_document.get("pages", []))
    if not pages:
        return ""
    first_page = pages[0]
    chunks: List[str] = []
    for block in first_page.get("text_blocks", []):
        text = str(block.get("text", "")).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)


def extract_datasheet_identifiers(
    normalized_document: Dict[str, Any],
    source_pdf: Path,
) -> Dict[str, Any]:
    metadata = normalized_document.get("metadata", {})
    first_page_text = _first_page_text(normalized_document)
    metadata_text = " ".join(
        [
            str(metadata.get("title", "")),
            str(metadata.get("subject", "")),
            str(metadata.get("keywords", "")),
            str(metadata.get("author", "")),
            str(source_pdf.stem),
        ]
    )

    tokens = _candidate_tokens(f"{metadata_text} {first_page_text}")
    normalized = sorted({_normalize_identifier(token) for token in tokens if _normalize_identifier(token)})
    search_blob_norm = _normalize_identifier(f"{metadata_text} {first_page_text}")
    return {
        "identifier_tokens": tokens,
        "identifier_norms": normalized,
        "search_blob_norm": search_blob_norm,
        "first_page_excerpt": first_page_text[:400],
    }


def _score_datasheet_match(part_norm: str, datasheet: Dict[str, Any]) -> Tuple[int, str]:
    norms = set(datasheet.get("identifier_norms", []))
    if part_norm and part_norm in norms:
        return 3, "exact_identifier_token"
    blob = str(datasheet.get("search_blob_norm", ""))
    if part_norm and part_norm in blob:
        return 2, "part_number_found_in_metadata_or_first_page"
    return 0, "no_match"


def link_components_to_datasheets(
    bom_crosswalk: Dict[str, Any],
    datasheets: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    datasheet_rows = [dict(row) for row in datasheets]
    by_ref = bom_crosswalk.get("by_ref", {})
    linked_refs_by_datasheet: Dict[str, List[str]] = {
        str(row.get("datasheet_id")): [] for row in datasheet_rows
    }
    component_links: List[Dict[str, Any]] = []

    for ref, ref_row in sorted(by_ref.items()):
        bom = ref_row.get("bom", {})
        part_number = str(bom.get("part_number", "")).strip()
        part_norm = _normalize_identifier(part_number)
        if not part_norm:
            component_links.append(
                {
                    "ref": ref,
                    "component_id": ref_row.get("component_id"),
                    "manufacturer": bom.get("manufacturer"),
                    "part_number": part_number,
                    "status": "unmatched",
                    "reason": "missing_part_number",
                    "candidates": [],
                    "linked_datasheet_id": None,
                    "linked_datasheet_markdown": None,
                }
            )
            continue

        candidates: List[Dict[str, Any]] = []
        for row in datasheet_rows:
            score, reason = _score_datasheet_match(part_norm, row)
            if score <= 0:
                continue
            candidates.append(
                {
                    "datasheet_id": row.get("datasheet_id"),
                    "datasheet_markdown": row.get("datasheet_markdown"),
                    "score": score,
                    "reason": reason,
                }
            )

        if not candidates:
            component_links.append(
                {
                    "ref": ref,
                    "component_id": ref_row.get("component_id"),
                    "manufacturer": bom.get("manufacturer"),
                    "part_number": part_number,
                    "status": "unmatched",
                    "reason": "no_datasheet_match",
                    "candidates": [],
                    "linked_datasheet_id": None,
                    "linked_datasheet_markdown": None,
                }
            )
            continue

        candidates.sort(key=lambda row: (-int(row["score"]), str(row["datasheet_id"])))
        best_score = int(candidates[0]["score"])
        best_rows = [row for row in candidates if int(row["score"]) == best_score]

        if len(best_rows) > 1:
            component_links.append(
                {
                    "ref": ref,
                    "component_id": ref_row.get("component_id"),
                    "manufacturer": bom.get("manufacturer"),
                    "part_number": part_number,
                    "status": "ambiguous",
                    "reason": "multiple_datasheets_with_same_confidence",
                    "candidates": candidates,
                    "linked_datasheet_id": None,
                    "linked_datasheet_markdown": None,
                }
            )
            continue

        winner = best_rows[0]
        linked_id = str(winner["datasheet_id"])
        linked_refs_by_datasheet.setdefault(linked_id, []).append(ref)
        component_links.append(
            {
                "ref": ref,
                "component_id": ref_row.get("component_id"),
                "manufacturer": bom.get("manufacturer"),
                "part_number": part_number,
                "status": "linked_exact" if best_score >= 3 else "linked_heuristic",
                "reason": winner["reason"],
                "candidates": candidates,
                "linked_datasheet_id": linked_id,
                "linked_datasheet_markdown": winner["datasheet_markdown"],
            }
        )

    for row in datasheet_rows:
        datasheet_id = str(row.get("datasheet_id"))
        linked_refs = sorted(linked_refs_by_datasheet.get(datasheet_id, []))
        row["linked_refs"] = linked_refs
        row["link_status"] = "linked" if linked_refs else "unlinked"

    summary = {
        "component_count": len(component_links),
        "linked_exact": sum(1 for row in component_links if row.get("status") == "linked_exact"),
        "linked_heuristic": sum(1 for row in component_links if row.get("status") == "linked_heuristic"),
        "ambiguous": sum(1 for row in component_links if row.get("status") == "ambiguous"),
        "unmatched": sum(1 for row in component_links if row.get("status") == "unmatched"),
        "datasheet_count": len(datasheet_rows),
        "linked_datasheet_count": sum(1 for row in datasheet_rows if row.get("link_status") == "linked"),
        "unlinked_datasheet_count": sum(1 for row in datasheet_rows if row.get("link_status") == "unlinked"),
    }
    return {
        "schema_version": "1.0.0",
        "summary": summary,
        "components": component_links,
        "datasheets": datasheet_rows,
    }
