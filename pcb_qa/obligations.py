from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Any


@dataclass
class EvidenceObligations:
    intent: str
    entities_required: dict[str, list[str]]
    relations_required: list[dict[str, Any]]
    acceptable_sources: list[str]
    minimum_confidence: str
    negative_checks: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_intent(question: str) -> str:
    q = question.lower()
    if any(token in q for token in ("jtag", "swd", "programming header", "vtref", "nreset", "debug header")):
        return "protocol_debug_validation"
    if any(token in q for token in ("floating", "misconnect", "wrong connection", "short", "disconnect")):
        return "anomaly_check"
    if any(token in q for token in ("pin", "vdd", "vddio", "connected correctly", "is connected")):
        return "pin_validation"
    if any(token in q for token in ("net", "which pins are on", "trace", "path")):
        return "net_trace"
    if any(token in q for token in ("communicate", "interface", "between", "how does")):
        return "relationship_trace"
    return "system_function"


def _extract_explicit_refs(question: str) -> list[str]:
    refs = set(re.findall(r"\b[A-Za-z]{1,4}\d{1,3}\b", question))
    return sorted(ref.upper() for ref in refs)


def derive_obligations(question: str, parser_entities: dict[str, Any] | None = None) -> EvidenceObligations:
    parser_entities = parser_entities or {}
    intent = classify_intent(question)
    required_refs = sorted(set(_extract_explicit_refs(question)) | set(parser_entities.get("refdes", [])))
    required_nets = sorted(set(parser_entities.get("nets", [])))
    relations: list[dict[str, Any]] = []
    negative_checks: list[str] = []
    acceptable_sources = ["DSN", "schematic", "datasheet", "BOM", "inference"]
    minimum_confidence = "high"

    if intent == "protocol_debug_validation":
        # Generalizable protocol requirement template for SWD/JTAG style debug headers.
        required_nets = sorted(set(required_nets) | {"SWDIO", "SWDCLK", "RESET", "GND", "1V8"})
        relations.extend(
            [
                {"type": "maps_to_header", "subject": "SWDIO", "object": "debug_header"},
                {"type": "maps_to_header", "subject": "SWDCLK", "object": "debug_header"},
                {"type": "maps_to_header", "subject": "RESET", "object": "debug_header"},
                {"type": "power_reference_present", "subject": "debug_header", "object": "1V8"},
                {"type": "ground_reference_present", "subject": "debug_header", "object": "GND"},
            ]
        )
        negative_checks = [
            "if required protocol nets are not confirmed in DSN, classify as unresolved_not_missing",
            "do not claim missing VTREF/GND without explicit DSN search for header pins",
        ]
        minimum_confidence = "exact"
    elif intent == "pin_validation":
        relations.append({"type": "pin_to_net_validation", "subject": "target_pin", "object": "expected_net"})
        minimum_confidence = "exact"
    elif intent == "anomaly_check":
        relations.append({"type": "anomaly_presence_check", "subject": "target_scope", "object": "known_anomalies"})
        minimum_confidence = "high"
    elif intent == "net_trace":
        relations.append({"type": "trace_path", "subject": "seed_nets_or_pins", "object": "neighboring_graph"})
    elif intent == "relationship_trace":
        relations.append({"type": "cross_block_connectivity", "subject": "block_a", "object": "block_b"})
    else:  # system_function
        relations.extend(
            [
                {"type": "block_presence", "subject": "power", "object": "compute_or_io"},
                {"type": "inter_block_path", "subject": "control", "object": "peripheral_or_connector"},
            ]
        )
        negative_checks = [
            "do not finalize with single-net-dominated evidence",
            "must include at least one power-domain and one io/debug trace",
        ]

    return EvidenceObligations(
        intent=intent,
        entities_required={"components": required_refs, "nets": required_nets, "pins": []},
        relations_required=relations,
        acceptable_sources=acceptable_sources,
        minimum_confidence=minimum_confidence,
        negative_checks=negative_checks,
    )


def evaluate_obligations(
    obligations: dict[str, Any],
    resolved_entities: dict[str, list[str]],
    evidence_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    required = obligations.get("entities_required", {})
    required_components = {str(item).upper() for item in required.get("components", [])}
    required_nets = {str(item).upper() for item in required.get("nets", [])}
    found_components = {str(item).upper() for item in resolved_entities.get("components", [])}
    found_nets = {str(item).upper() for item in resolved_entities.get("nets", [])}

    missing_components = sorted(required_components - found_components)
    missing_nets = sorted(required_nets - found_nets)
    missing_relations: list[str] = []
    relations_required = obligations.get("relations_required", [])

    evidence_types = [str(row.get("type", "")) for row in evidence_rows]
    if obligations.get("intent") == "protocol_debug_validation":
        # Require explicit DSN net evidence rows for protocol net set.
        dsn_nets = {
            str(row.get("data", {}).get("net_name_canonical", "")).upper()
            for row in evidence_rows
            if row.get("type") == "net"
        }
        for token in ("SWDIO", "SWDCLK", "RESET", "GND"):
            if token not in dsn_nets:
                missing_relations.append(f"required_debug_net_not_evidenced:{token}")
        # VTREF can be board-specific; here we allow 1V8 or any VDD* net.
        has_vtref = any(net.startswith("1V8") or net.startswith("VDD") for net in dsn_nets)
        if not has_vtref:
            missing_relations.append("required_debug_power_reference_not_evidenced")
    elif obligations.get("intent") == "system_function":
        if "function_block" not in evidence_types:
            missing_relations.append("missing_function_block_evidence")
        if len(found_nets) < 5:
            missing_relations.append("insufficient_net_diversity")

    coverage_score = 1.0
    total_required = len(required_components) + len(required_nets) + len(relations_required)
    total_missing = len(missing_components) + len(missing_nets) + len(missing_relations)
    if total_required > 0:
        coverage_score = max(0.0, 1.0 - (total_missing / total_required))

    return {
        "intent": obligations.get("intent", "unknown"),
        "required_entities": required,
        "missing_obligations": {
            "components": missing_components,
            "nets": missing_nets,
            "relations": missing_relations,
        },
        "coverage_score": round(coverage_score, 4),
        "coverage_satisfied": not (missing_components or missing_nets or missing_relations),
    }

