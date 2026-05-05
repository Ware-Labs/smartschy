from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import write_json


SOURCE_PRIORITY_ORDER = {
    "DSN": 1,
    "schematic": 2,
    "datasheet": 3,
    "BOM": 4,
    "inference": 5,
}


def normalize_source_priority(raw_priority: str | None) -> str:
    if not raw_priority:
        return "inference"
    token = raw_priority.strip()
    if token.upper() == "DSN":
        return "DSN"
    lowered = token.lower()
    if lowered in {"schematic", "datasheet", "bom", "inference"}:
        return lowered if lowered != "bom" else "BOM"
    return "inference"


def default_confidence_for_priority(priority: str) -> str:
    if priority == "DSN":
        return "exact"
    if priority in {"schematic", "datasheet"}:
        return "high"
    if priority == "BOM":
        return "medium"
    return "low"


def _normalize_evidence_row(idx: int, row: dict[str, Any]) -> dict[str, Any]:
    priority = normalize_source_priority(str(row.get("source_priority", "")))
    evidence_id = row.get("id") or f"ev_{idx:04d}"
    out = {
        "id": evidence_id,
        "type": row.get("type", "unknown"),
        "source_priority": priority,
        "claim_supported": row.get("claim_supported", ""),
        "data": row.get("data", {}),
        "source": row.get("source", {}),
        "confidence": row.get("confidence", default_confidence_for_priority(priority)),
        "limitations": row.get("limitations", []),
        "tool_call_ids": row.get("tool_call_ids", []),
    }
    return out


def _sort_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key_fn(row: dict[str, Any]) -> tuple[int, str]:
        priority = normalize_source_priority(row.get("source_priority"))
        return (SOURCE_PRIORITY_ORDER.get(priority, 999), str(row.get("id", "")))

    return sorted(rows, key=key_fn)


def build_evidence_packet(
    project_root: Path,
    question: str,
    selected_evidence: list[dict[str, Any]],
    agent_trace: dict[str, Any] | None = None,
    resolved_entities: dict[str, Any] | None = None,
    open_uncertainties: list[str] | None = None,
    critical_findings: list[str] | None = None,
    recommended_answer_constraints: list[str] | None = None,
    limits: dict[str, Any] | None = None,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    normalized = [_normalize_evidence_row(i + 1, row) for i, row in enumerate(selected_evidence)]
    normalized = _sort_evidence(normalized)
    packet = {
        "question": question,
        "project": {
            "project_root": str(project_root.resolve()),
        },
        "agent_trace": {
            "iterations": (agent_trace or {}).get("iterations", []),
            "stop_reason": stop_reason or (agent_trace or {}).get("stop_reason", ""),
            "limits": limits or (agent_trace or {}).get("limits", {}),
        },
        "resolved_entities": resolved_entities or {
            "components": [],
            "nets": [],
            "pins": [],
            "datasheets": [],
            "schematic_pages": [],
        },
        "evidence_priority": [
            "DSN",
            "schematic",
            "datasheet",
            "BOM",
            "inference",
        ],
        "evidence": normalized,
        "open_uncertainties": open_uncertainties or [],
        "critical_findings": critical_findings or [],
        "functional_hypotheses": [],
        "anomaly_findings": [],
        "evidence_diversity_metrics": {},
        "recommended_answer_constraints": recommended_answer_constraints or [],
    }
    return packet


def write_evidence_packet(path: Path, packet: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, packet)
