#!/usr/bin/env python3
"""Build a second-pass-answerable LLM summary from derived board artifacts."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from bom_ingest import classify_component


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tokenize_question(question: str) -> List[str]:
    return [t.upper() for t in re.findall(r"[A-Za-z0-9_./+-]+", question)]


def _matched_nets(question_tokens: Sequence[str], net_names: Sequence[str]) -> List[str]:
    matched: Set[str] = set()
    nets_u = {n.upper(): n for n in net_names}
    for token in question_tokens:
        if token in nets_u:
            matched.add(nets_u[token])
            continue
        for n in net_names:
            up = n.upper()
            if token in up or up in token:
                matched.add(n)
    return sorted(matched)


def _matched_refs(question_tokens: Sequence[str], refs: Sequence[str]) -> List[str]:
    matched: Set[str] = set()
    refs_u = {r.upper(): r for r in refs}
    for token in question_tokens:
        if token in refs_u:
            matched.add(refs_u[token])
            continue
        for r in refs:
            up = r.upper()
            if token == up or (len(token) >= 3 and token in up):
                matched.add(r)
    return sorted(matched)


def _split_pin_token(pin_token: str) -> Dict[str, Optional[str]]:
    if "-" not in pin_token:
        return {"ref": pin_token, "pin_number": None}
    ref, pin = pin_token.split("-", 1)
    return {"ref": ref, "pin_number": pin}


def _is_connector_ref(ref: str) -> bool:
    up = ref.upper()
    return up.startswith("J") or up.startswith("P")


def _is_module_or_ic_ref(ref: str) -> bool:
    up = ref.upper()
    return up.startswith("U") or up.startswith("MOD")


def _component_name(ref_row: Dict[str, Any]) -> str:
    bom = ref_row.get("bom", {})
    return str(bom.get("name") or ref_row.get("component_id") or "unknown")


def _infer_expected_mapping(focus_nets: Sequence[str], question_tokens: Sequence[str]) -> Dict[str, Any]:
    expected: Dict[str, Any] = {}
    debug_required = []
    has_swd = any("SWD" in t for t in question_tokens) or any("SWD" in n.upper() for n in focus_nets)
    if has_swd:
        debug_required = ["SWDIO", "SWDCLK", "RESET", "VTREF", "GND"]

    for net in focus_nets:
        up = net.upper()
        expected_function = None
        if "SWDIO" in up:
            expected_function = "SWDIO"
        elif "SWDCLK" in up:
            expected_function = "SWDCLK"
        elif "RESET" in up or "R\\E\\S\\E\\T" in up:
            expected_function = "RESET"
        elif "VTREF" in up or "VREF" in up:
            expected_function = "VTREF"
        elif up == "GND":
            expected_function = "GND"
        else:
            expected_function = net
        expected[net] = {
            "expected_function": expected_function,
            "expected_endpoint_classes": ["ic_or_module", "connector"],
            "confidence": "heuristic_from_net_name",
        }

    return {"expected_mapping": expected, "related_required_nets": debug_required}


def _debug_signal_candidates(signals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    debug_re = re.compile(r"(SWD|JTAG|RESET|R\\E\\S\\E\\T\\|BOOT|UART|I2C|SPI|CLK|DIO)", re.IGNORECASE)
    out = []
    for signal in signals:
        net = signal.get("net", "")
        endpoints = signal.get("endpoint_components", [])
        if debug_re.search(net):
            out.append(
                {
                    "net": net,
                    "fanout": signal.get("fanout"),
                    "endpoints": endpoints,
                    "neighbors": signal.get("neighbor_nets", []),
                }
            )
            continue
        if any(str(ref).upper().startswith("TP") for ref in endpoints):
            out.append(
                {
                    "net": net,
                    "fanout": signal.get("fanout"),
                    "endpoints": endpoints,
                    "neighbors": signal.get("neighbor_nets", []),
                }
            )
    return sorted(out, key=lambda row: str(row["net"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build llm_summary.json from derived artifacts.")
    parser.add_argument(
        "--board",
        required=True,
        help="Path to board derived artifact folder (contains 01/02/04/07 files).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output llm_summary.json path.",
    )
    parser.add_argument(
        "--question",
        default="",
        help="Optional user question used to precompute focus candidates.",
    )
    return parser.parse_args()


def build_summary(board_dir: Path, question: str) -> Dict[str, Any]:
    connectivity = _read_json(board_dir / "01_connectivity.core.json")
    components = _read_json(board_dir / "02_components.catalog.json")
    signal_views = _read_json(board_dir / "04_signal_views.json")
    crosswalk = _read_json(board_dir / "07_bom_crosswalk.json")

    net_rows = signal_views.get("signals", [])
    net_names = [row.get("net", "") for row in net_rows]
    refs = [row.get("ref", "") for row in components.get("components", [])]
    net_index_raw = {row.get("name"): row for row in connectivity.get("nets", [])}
    pin_to_net = connectivity.get("indexes", {}).get("pin_to_net", {})

    question_tokens = _tokenize_question(question)
    focus_nets = _matched_nets(question_tokens, net_names)
    focus_refs = _matched_refs(question_tokens, refs)
    inferred = _infer_expected_mapping(focus_nets, question_tokens)

    net_index: Dict[str, Any] = {}
    path_hints: Dict[str, Any] = {}
    for row in net_rows:
        net = row["net"]
        net_index[net] = {
            "pins_unique_count": row.get("pin_count_unique"),
            "fanout": row.get("fanout"),
            "endpoint_refs": row.get("endpoint_components", []),
            "neighbor_nets": row.get("neighbor_nets", []),
            "endpoint_part_classes": row.get("endpoint_part_classes", {}),
        }
        route = row.get("route", {})
        path_hints[net] = {
            "wire_count": route.get("wire_count", 0),
            "via_count": route.get("via_count", 0),
            "route_component_count": route.get("route_component_count", 0),
            "layers": route.get("layers", []),
            "wire_length_estimate": route.get("wire_length_estimate", 0.0),
        }

    ref_index: Dict[str, Any] = {}
    for comp in components.get("components", []):
        ref = comp["ref"]
        bom = comp.get("bom", {})
        ref_index[ref] = {
            "component_id": comp.get("component_id"),
            "connected_nets": comp.get("connected_nets", []),
            "pins_in_nets": comp.get("pins_in_nets", []),
            "component_name": _component_name(comp),
            "component_class": classify_component(ref, str(bom.get("name") or "")),
            "bom": {
                "matched": bom.get("matched", False),
                "name": bom.get("name"),
                "manufacturer": bom.get("manufacturer"),
                "part_number": bom.get("part_number"),
                "footprint": bom.get("footprint"),
            },
        }

    pin_level_evidence: Dict[str, Any] = {}
    focus_component_catalog: Dict[str, Any] = {}
    connector_pinouts: Dict[str, Any] = {}
    endpoint_role_classification: Dict[str, Any] = {}
    actual_mapping: Dict[str, Any] = {}
    answerability_inputs: Dict[str, Any] = {}

    for net in focus_nets:
        pins_unique = net_index_raw.get(net, {}).get("pins_unique", [])
        evidence_rows = []
        endpoint_roles = []
        has_pin_numbers = True
        has_pin_functions = True
        has_connector = False
        connector_pinout_present = True
        endpoints = net_index.get(net, {}).get("endpoint_refs", [])
        expected_function = inferred["expected_mapping"].get(net, {}).get("expected_function")
        actual_function_hits: List[str] = []
        swap_hits: List[str] = []

        for pin_token in pins_unique:
            split = _split_pin_token(pin_token)
            ref = split["ref"] or ""
            pin_num = split["pin_number"]
            ref_row = ref_index.get(ref, {})
            comp_class = ref_row.get("component_class", classify_component(ref, ""))
            comp_name = ref_row.get("component_name", ref_row.get("component_id", "unknown"))
            pin_function = None
            missing_reason = []
            if pin_num is None:
                has_pin_numbers = False
                missing_reason.append("pin_number_unavailable_from_token")
            if pin_function is None:
                has_pin_functions = False
                missing_reason.append("pin_function_not_available_in_source_artifacts")

            if _is_connector_ref(ref):
                has_connector = True
            if expected_function and pin_function:
                pin_up = str(pin_function).upper()
                if expected_function.upper() in pin_up:
                    actual_function_hits.append(pin_token)
                if expected_function.upper() == "SWDIO" and "SWDCLK" in pin_up:
                    swap_hits.append(pin_token)
                if expected_function.upper() == "SWDCLK" and "SWDIO" in pin_up:
                    swap_hits.append(pin_token)

            evidence_rows.append(
                {
                    "net": net,
                    "ref": ref,
                    "component_name": comp_name,
                    "component_class": comp_class,
                    "manufacturer": ref_row.get("bom", {}).get("manufacturer"),
                    "part_number": ref_row.get("bom", {}).get("part_number"),
                    "pin_token": pin_token,
                    "pin_number": pin_num,
                    "pin_name_or_pad": pin_num,
                    "pin_function": pin_function,
                    "electrical_type": None,
                    "sheet_path": None,
                    "missing_reason": missing_reason,
                }
            )

            if comp_class in ("ic_or_module", "connector"):
                role = "intended_endpoint"
            elif comp_class in ("resistor", "capacitor", "inductor_or_bead", "diode_or_led", "testpoint", "switch"):
                role = "intermediate_or_support"
            else:
                role = "unknown_or_accidental"
            endpoint_roles.append(
                {
                    "ref": ref,
                    "role": role,
                    "reason": f"classified_as_{comp_class}",
                }
            )
            focus_component_catalog[ref] = {
                "ref": ref,
                "component_name": comp_name,
                "component_class": comp_class,
                "manufacturer": ref_row.get("bom", {}).get("manufacturer"),
                "part_number": ref_row.get("bom", {}).get("part_number"),
            }

        for ref in endpoints:
            if not _is_connector_ref(ref):
                continue
            connector_pins = sorted(
                p for p in ref_index.get(ref, {}).get("pins_in_nets", []) if p.startswith(f"{ref}-")
            )
            if not connector_pins:
                connector_pinout_present = False
            connector_pinouts[ref] = {
                "ref": ref,
                "component_name": ref_index.get(ref, {}).get("component_name"),
                "component_id": ref_index.get(ref, {}).get("component_id"),
                "manufacturer": ref_index.get(ref, {}).get("bom", {}).get("manufacturer"),
                "part_number": ref_index.get(ref, {}).get("bom", {}).get("part_number"),
                "pins": [
                    {
                        "pin_token": pin,
                        "pin_number": _split_pin_token(pin)["pin_number"],
                        "pin_name": None,
                        "connected_net": pin_to_net.get(pin),
                    }
                    for pin in connector_pins
                ],
            }

        expected = inferred["expected_mapping"].get(net, {})
        mapping_status = "unknown"
        if swap_hits:
            mapping_status = "mismatch_swap_suspected"
        elif actual_function_hits:
            mapping_status = "match"
        elif has_pin_functions:
            mapping_status = "mismatch_or_unknown"
        else:
            mapping_status = "unknown_no_pin_function_data"

        actual_mapping[net] = {
            "expected_function": expected.get("expected_function"),
            "actual_function_hits": actual_function_hits,
            "swap_hits": swap_hits,
            "status": mapping_status,
            "confidence": "low_if_no_pin_function_data" if not has_pin_functions else "medium",
        }

        missing_evidence = []
        if not has_pin_numbers:
            missing_evidence.append("missing_pin_numbers")
        if not has_pin_functions:
            missing_evidence.append("missing_pin_functions")
        if has_connector and not connector_pinout_present:
            missing_evidence.append("missing_connector_pinout")

        answerability_inputs[net] = {
            "has_pin_numbers": has_pin_numbers,
            "has_pin_functions": has_pin_functions,
            "has_connector_endpoint": has_connector,
            "has_connector_pinout": connector_pinout_present,
            "missing_evidence": missing_evidence,
        }

        pin_level_evidence[net] = evidence_rows
        endpoint_role_classification[net] = sorted(endpoint_roles, key=lambda x: (x["ref"], x["role"]))

    related_required_nets = {
        "required": inferred.get("related_required_nets", []),
        "present": [n for n in inferred.get("related_required_nets", []) if n in net_index],
        "missing": [n for n in inferred.get("related_required_nets", []) if n not in net_index],
    }

    summary = {
        "schema_version": "1.0.0",
        "board": {
            "name": board_dir.name,
            "source": connectivity.get("source", {}),
        },
        "question": {
            "original": question,
            "tokens": question_tokens,
            "question_focus_candidates": {
                "nets": focus_nets,
                "refs": focus_refs,
            },
        },
        "net_index": dict(sorted(net_index.items())),
        "ref_index": dict(sorted(ref_index.items())),
        "focus_component_catalog": dict(sorted(focus_component_catalog.items())),
        "path_hints": dict(sorted(path_hints.items())),
        "pin_level_evidence": dict(sorted(pin_level_evidence.items())),
        "expected_mapping": inferred["expected_mapping"],
        "actual_mapping": dict(sorted(actual_mapping.items())),
        "connector_pinouts": dict(sorted(connector_pinouts.items())),
        "related_required_nets": related_required_nets,
        "endpoint_role_classification": dict(sorted(endpoint_role_classification.items())),
        "answerability_inputs": dict(sorted(answerability_inputs.items())),
        "debug_signals": _debug_signal_candidates(net_rows),
        "crosswalk_summary": crosswalk.get("summary", {}),
        "provenance": {
            "artifacts": {
                "connectivity": str(board_dir / "01_connectivity.core.json"),
                "components": str(board_dir / "02_components.catalog.json"),
                "signal_views": str(board_dir / "04_signal_views.json"),
                "crosswalk": str(board_dir / "07_bom_crosswalk.json"),
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
    return summary


def main() -> None:
    args = parse_args()
    board_dir = Path(args.board)
    out_path = Path(args.out)
    summary = build_summary(board_dir, args.question)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
