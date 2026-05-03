#!/usr/bin/env python3
"""Build board review artifacts from normalized DSN + BOM."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from bom_ingest import classify_component


ARTIFACT_FILE_ORDER = [
    ("01_connectivity.core.json", "connectivity"),
    ("02_components.catalog.json", "components"),
    ("03_routing.topology.json", "routing"),
    ("04_signal_views.json", "signal_views"),
    ("05_integrity_checks.json", "integrity_checks"),
    ("06_review_report.md", "review_report_md"),
    ("07_bom_crosswalk.json", "bom_crosswalk"),
]


def _split_pin(pin_token: str) -> Tuple[str, str]:
    if "-" not in pin_token:
        return pin_token, ""
    return pin_token.split("-", 1)


def _distance(points: Iterable[List[float]]) -> float:
    seq = list(points)
    if len(seq) < 2:
        return 0.0
    acc = 0.0
    for i in range(1, len(seq)):
        x1, y1 = seq[i - 1]
        x2, y2 = seq[i]
        acc += math.hypot(float(x2) - float(x1), float(y2) - float(y1))
    return round(acc, 4)


def _build_bom_crosswalk(normalized: Dict[str, Any], bom_model: Dict[str, Any]) -> Dict[str, Any]:
    bom_rows = bom_model.get("rows", [])
    ref_to_rows_idx = bom_model.get("indexes", {}).get("ref_to_rows", {})
    row_by_id = {row.get("row_id"): row for row in bom_rows}
    dsn_components = normalized.get("components", [])
    dsn_by_ref = {c.get("ref"): c for c in dsn_components if c.get("ref")}

    by_ref: Dict[str, Any] = {}
    matched = 0
    for ref, comp in sorted(dsn_by_ref.items()):
        row_ids = ref_to_rows_idx.get(ref, [])
        rows = [row_by_id[rid] for rid in row_ids if rid in row_by_id]
        bom_top = rows[0] if rows else {}
        if rows:
            matched += 1
        by_ref[ref] = {
            "ref": ref,
            "component_id": comp.get("component_id"),
            "dsn_component": comp,
            "bom_matched": bool(rows),
            "bom_row_ids": row_ids,
            "bom": {
                "name": bom_top.get("name"),
                "manufacturer": bom_top.get("manufacturer"),
                "part_number": bom_top.get("part_number"),
                "footprint": bom_top.get("footprint"),
                "quantity": bom_top.get("quantity"),
            },
        }

    bom_only_refs = sorted(ref for ref in ref_to_rows_idx.keys() if ref not in dsn_by_ref)
    return {
        "schema_version": "1.0.0",
        "summary": {
            "dsn_component_count": len(dsn_by_ref),
            "bom_row_count": len(bom_rows),
            "matched_ref_count": matched,
            "bom_only_ref_count": len(bom_only_refs),
        },
        "by_ref": by_ref,
        "bom_only_refs": bom_only_refs,
    }


def _build_connectivity(normalized: Dict[str, Any]) -> Dict[str, Any]:
    nets = normalized.get("nets", [])
    pin_to_net: Dict[str, str] = {}
    ref_to_pins: Dict[str, List[str]] = defaultdict(list)
    ref_to_nets: Dict[str, List[str]] = defaultdict(list)
    out_nets = []

    for net in nets:
        name = net.get("name", "")
        pins_unique = list(net.get("pins_unique", []))
        refs = sorted({_split_pin(p)[0] for p in pins_unique if p})
        for p in pins_unique:
            pin_to_net[p] = name
            ref, _ = _split_pin(p)
            if p not in ref_to_pins[ref]:
                ref_to_pins[ref].append(p)
            if name not in ref_to_nets[ref]:
                ref_to_nets[ref].append(name)
        out_nets.append(
            {
                "name": name,
                "pins": list(net.get("pins", [])),
                "pins_unique": pins_unique,
                "duplicate_pins": list(net.get("duplicate_pins", [])),
                "pin_count_unique": len(pins_unique),
                "fanout": len(refs),
                "endpoint_refs": refs,
            }
        )

    return {
        "schema_version": "1.0.0",
        "source": normalized.get("source", {}),
        "nets": out_nets,
        "indexes": {
            "pin_to_net": dict(sorted(pin_to_net.items())),
            "ref_to_pins": {k: sorted(v) for k, v in sorted(ref_to_pins.items())},
            "ref_to_nets": {k: sorted(v) for k, v in sorted(ref_to_nets.items())},
        },
    }


def _build_components_catalog(
    normalized: Dict[str, Any], connectivity: Dict[str, Any], crosswalk: Dict[str, Any]
) -> Dict[str, Any]:
    ref_to_nets = connectivity.get("indexes", {}).get("ref_to_nets", {})
    ref_to_pins = connectivity.get("indexes", {}).get("ref_to_pins", {})
    by_ref = crosswalk.get("by_ref", {})

    comps = []
    for comp in normalized.get("components", []):
        ref = comp.get("ref")
        bom_row = by_ref.get(ref, {})
        bom = bom_row.get("bom", {})
        comps.append(
            {
                "ref": ref,
                "component_id": comp.get("component_id"),
                "x": comp.get("x"),
                "y": comp.get("y"),
                "rotation": comp.get("rotation"),
                "side": comp.get("side"),
                "connected_nets": ref_to_nets.get(ref, []),
                "pins_in_nets": ref_to_pins.get(ref, []),
                "component_class": classify_component(ref or "", str(bom.get("name") or "")),
                "bom": {
                    "matched": bool(bom_row.get("bom_matched")),
                    "name": bom.get("name"),
                    "manufacturer": bom.get("manufacturer"),
                    "part_number": bom.get("part_number"),
                    "footprint": bom.get("footprint"),
                },
            }
        )

    return {"schema_version": "1.0.0", "components": comps}


def _build_routing_topology(normalized: Dict[str, Any], connectivity: Dict[str, Any]) -> Dict[str, Any]:
    wires = normalized.get("wiring", {}).get("wires", [])
    vias = normalized.get("wiring", {}).get("vias", [])
    by_net: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"wire_count": 0, "via_count": 0, "layers": set(), "wire_length_estimate": 0.0}
    )
    for w in wires:
        net = str(w.get("net", ""))
        row = by_net[net]
        row["wire_count"] += 1
        row["layers"].add(str(w.get("layer", "")))
        row["wire_length_estimate"] += _distance(w.get("points", []))
    for v in vias:
        net = str(v.get("net", ""))
        by_net[net]["via_count"] += 1

    endpoint_count = {
        n.get("name"): len(set(n.get("endpoint_refs", []))) for n in connectivity.get("nets", [])
    }
    nets = []
    for net, row in sorted(by_net.items()):
        nets.append(
            {
                "net": net,
                "wire_count": row["wire_count"],
                "via_count": row["via_count"],
                "layers": sorted(x for x in row["layers"] if x),
                "wire_length_estimate": round(float(row["wire_length_estimate"]), 4),
                "route_component_count": endpoint_count.get(net, 0),
            }
        )
    return {"schema_version": "1.0.0", "nets": nets}


def _build_signal_views(
    connectivity: Dict[str, Any], routing: Dict[str, Any], components: Dict[str, Any]
) -> Dict[str, Any]:
    route_by_net = {r.get("net"): r for r in routing.get("nets", [])}
    comp_by_ref = {c.get("ref"): c for c in components.get("components", [])}
    ref_to_nets = connectivity.get("indexes", {}).get("ref_to_nets", {})
    out = []
    for net in connectivity.get("nets", []):
        net_name = net.get("name")
        endpoints = list(net.get("endpoint_refs", []))
        neighbor = set()
        class_count: Dict[str, int] = defaultdict(int)
        for ref in endpoints:
            comp = comp_by_ref.get(ref, {})
            cls = comp.get("component_class", "other")
            class_count[cls] += 1
            for n in ref_to_nets.get(ref, []):
                if n != net_name:
                    neighbor.add(n)
        out.append(
            {
                "net": net_name,
                "pin_count_unique": net.get("pin_count_unique", 0),
                "fanout": net.get("fanout", 0),
                "endpoint_components": endpoints,
                "neighbor_nets": sorted(neighbor),
                "endpoint_part_classes": dict(sorted(class_count.items())),
                "route": route_by_net.get(
                    net_name,
                    {
                        "wire_count": 0,
                        "via_count": 0,
                        "layers": [],
                        "wire_length_estimate": 0.0,
                        "route_component_count": len(endpoints),
                    },
                ),
            }
        )
    return {"schema_version": "1.0.0", "signals": out}


def _build_integrity_checks(
    connectivity: Dict[str, Any], components: Dict[str, Any], crosswalk: Dict[str, Any]
) -> Dict[str, Any]:
    comp_refs = {c.get("ref") for c in components.get("components", [])}
    dangling_nets = [n.get("name") for n in connectivity.get("nets", []) if n.get("fanout", 0) <= 1]
    unmatched_components = sorted(
        ref for ref, row in crosswalk.get("by_ref", {}).items() if not row.get("bom_matched")
    )
    return {
        "schema_version": "1.0.0",
        "checks": {
            "dangling_nets": dangling_nets,
            "unmatched_components": unmatched_components,
            "component_count": len(comp_refs),
        },
    }


def _build_review_report(
    connectivity: Dict[str, Any], components: Dict[str, Any], crosswalk: Dict[str, Any]
) -> str:
    return "\n".join(
        [
            "# Board Review Report",
            "",
            f"- Nets: {len(connectivity.get('nets', []))}",
            f"- Components: {len(components.get('components', []))}",
            f"- BOM matched refs: {crosswalk.get('summary', {}).get('matched_ref_count', 0)}",
            "",
        ]
    )


def build_artifact_pack(normalized: Dict[str, Any], bom_model: Dict[str, Any]) -> Dict[str, Any]:
    connectivity = _build_connectivity(normalized)
    crosswalk = _build_bom_crosswalk(normalized, bom_model)
    components = _build_components_catalog(normalized, connectivity, crosswalk)
    routing = _build_routing_topology(normalized, connectivity)
    signal_views = _build_signal_views(connectivity, routing, components)
    checks = _build_integrity_checks(connectivity, components, crosswalk)
    report_md = _build_review_report(connectivity, components, crosswalk)
    return {
        "connectivity": connectivity,
        "components": components,
        "routing": routing,
        "signal_views": signal_views,
        "integrity_checks": checks,
        "review_report_md": report_md,
        "bom_crosswalk": crosswalk,
    }


def write_artifact_pack(pack: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, key in ARTIFACT_FILE_ORDER:
        target = out_dir / filename
        payload = pack[key]
        if filename.endswith(".md"):
            target.write_text(str(payload).strip() + "\n", encoding="utf-8")
        else:
            target.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
