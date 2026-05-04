#!/usr/bin/env python3
"""Parse Spectra DSN into normalized internal JSON schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def dump_json(payload: Dict[str, Any], compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)


def _to_float(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _to_int(value: Any) -> Any:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return value


def _tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in ("(", ")"):
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while i < n:
                if text[i] == '"' and (i == 0 or text[i - 1] != "\\"):
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            tokens.append("".join(buf))
            continue
        # comment skip
        if ch == ";":
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue
        j = i
        while j < n and (not text[j].isspace()) and text[j] not in ("(", ")"):
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def _parse_sexpr(tokens: List[str]) -> Any:
    idx = 0

    def parse_item() -> Any:
        nonlocal idx
        if idx >= len(tokens):
            raise ValueError("Unexpected end of DSN tokens")
        tok = tokens[idx]
        idx += 1
        if tok == "(":
            out = []
            while idx < len(tokens) and tokens[idx] != ")":
                out.append(parse_item())
            if idx >= len(tokens):
                raise ValueError("Unbalanced parentheses in DSN")
            idx += 1  # skip ')'
            return out
        if tok == ")":
            raise ValueError("Unexpected ')' in DSN")
        return tok

    result = parse_item()
    if idx != len(tokens):
        raise ValueError("Trailing tokens after root S-expression")
    return result


def _iter_sections(root: Any, tag: str) -> Iterable[List[Any]]:
    if not isinstance(root, list):
        return []
    return [node for node in root if isinstance(node, list) and node and node[0] == tag]


def _extract_resolution(root: List[Any]) -> Dict[str, Any]:
    unit_system = ""
    resolution: Any = None
    for section in _iter_sections(root, "resolution"):
        if len(section) >= 3:
            unit_system = str(section[1])
            resolution = _to_int(section[2])
            break
    return {"unit_system": unit_system, "resolution": resolution}


def _extract_placement(root: List[Any]) -> List[Dict[str, Any]]:
    components: List[Dict[str, Any]] = []
    for placement in _iter_sections(root, "placement"):
        for node in placement[1:]:
            if not (isinstance(node, list) and node and node[0] == "component"):
                continue
            component_id = str(node[1]) if len(node) > 1 else ""
            for sub in node[2:]:
                if not (isinstance(sub, list) and sub and sub[0] == "place"):
                    continue
                # (place REF X Y side rot)
                ref = str(sub[1]) if len(sub) > 1 else ""
                x = _to_float(sub[2]) if len(sub) > 2 else None
                y = _to_float(sub[3]) if len(sub) > 3 else None
                side = str(sub[4]) if len(sub) > 4 else None
                rotation = _to_float(sub[5]) if len(sub) > 5 else None
                components.append(
                    {
                        "component_id": component_id,
                        "ref": ref,
                        "x": x,
                        "y": y,
                        "side": side,
                        "rotation": rotation,
                    }
                )
    return components


def _extract_network(root: List[Any]) -> List[Dict[str, Any]]:
    nets: List[Dict[str, Any]] = []
    for network in _iter_sections(root, "network"):
        for node in network[1:]:
            if not (isinstance(node, list) and node and node[0] == "net"):
                continue
            name = str(node[1]) if len(node) > 1 else ""
            pins: List[str] = []
            for sub in node[2:]:
                if isinstance(sub, list) and sub and sub[0] == "pins":
                    for p in sub[1:]:
                        if isinstance(p, str):
                            pins.append(p)
            seen = set()
            pins_unique: List[str] = []
            dup_counts: Dict[str, int] = {}
            for p in pins:
                if p in seen:
                    dup_counts[p] = dup_counts.get(p, 1) + 1
                    continue
                seen.add(p)
                pins_unique.append(p)
            nets.append(
                {
                    "name": name,
                    "pins": pins,
                    "pins_unique": pins_unique,
                    "duplicate_pins": [{"pin": k, "count": v} for k, v in sorted(dup_counts.items())],
                }
            )
    return nets


def _extract_wiring(root: List[Any]) -> Dict[str, Any]:
    wires: List[Dict[str, Any]] = []
    vias: List[Dict[str, Any]] = []
    for wiring in _iter_sections(root, "wiring"):
        for node in wiring[1:]:
            if not (isinstance(node, list) and node):
                continue
            tag = node[0]
            if tag == "wire":
                row: Dict[str, Any] = {"layer": None, "width": None, "points": [], "net": None, "type": None}
                for sub in node[1:]:
                    if not (isinstance(sub, list) and sub):
                        continue
                    if sub[0] == "path":
                        row["layer"] = sub[1] if len(sub) > 1 else None
                        row["width"] = _to_float(sub[2]) if len(sub) > 2 else None
                        coords = sub[3:]
                        pts = []
                        for i in range(0, len(coords), 2):
                            if i + 1 >= len(coords):
                                break
                            pts.append([_to_float(coords[i]), _to_float(coords[i + 1])])
                        row["points"] = pts
                    elif sub[0] == "net":
                        row["net"] = sub[1] if len(sub) > 1 else None
                    elif sub[0] == "type":
                        row["type"] = sub[1] if len(sub) > 1 else None
                wires.append(row)
            elif tag == "via":
                # (via PADSTACK X Y (net N) (type normal))
                row = {
                    "padstack": node[1] if len(node) > 1 else None,
                    "x": _to_float(node[2]) if len(node) > 2 else None,
                    "y": _to_float(node[3]) if len(node) > 3 else None,
                    "net": None,
                    "type": None,
                }
                for sub in node[4:]:
                    if not (isinstance(sub, list) and sub):
                        continue
                    if sub[0] == "net":
                        row["net"] = sub[1] if len(sub) > 1 else None
                    elif sub[0] == "type":
                        row["type"] = sub[1] if len(sub) > 1 else None
                vias.append(row)
    return {"wires": wires, "vias": vias}


def _build_indexes(components: List[Dict[str, Any]], nets: List[Dict[str, Any]]) -> Dict[str, Any]:
    components_by_ref: Dict[str, Dict[str, Any]] = {}
    duplicate_refs: List[str] = []
    for c in components:
        ref = c.get("ref")
        if ref in components_by_ref:
            duplicate_refs.append(str(ref))
            continue
        components_by_ref[str(ref)] = c

    component_nets: Dict[str, List[str]] = {}
    net_pin_count: Dict[str, int] = {}
    for net in nets:
        n = str(net.get("name"))
        pin_u = net.get("pins_unique", [])
        net_pin_count[n] = len(pin_u)
        for p in pin_u:
            ref = str(p).split("-", 1)[0]
            component_nets.setdefault(ref, [])
            if n not in component_nets[ref]:
                component_nets[ref].append(n)

    return {
        "components_by_ref": components_by_ref,
        "component_nets": {k: sorted(v) for k, v in sorted(component_nets.items())},
        "net_pin_count": dict(sorted(net_pin_count.items())),
        "duplicate_component_refs": sorted(duplicate_refs),
    }


def normalize_dsn_text(text: str, source_path: str = "") -> Dict[str, Any]:
    # Accept already-normalized JSON if provided.
    try:
        as_json = json.loads(text)
        if isinstance(as_json, dict) and "nets" in as_json and "components" in as_json:
            return as_json
    except json.JSONDecodeError:
        pass

    sexpr = _parse_sexpr(_tokenize(text))
    if not isinstance(sexpr, list) or not sexpr or sexpr[0] != "pcb":
        raise ValueError(f"{source_path}: expected DSN root form '(pcb ...)'")

    board_name = str(sexpr[1]) if len(sexpr) > 1 else Path(source_path).stem
    root_sections = sexpr[2:]

    units = _extract_resolution(root_sections)
    components = _extract_placement(root_sections)
    nets = _extract_network(root_sections)
    wiring = _extract_wiring(root_sections)
    indexes = _build_indexes(components, nets)

    payload = {
        "schema_version": "1.0.0",
        "source": {"path": str(source_path), "format": "spectra_dsn", "board_name": board_name},
        "units": units,
        "components": components,
        "nets": nets,
        "wiring": wiring,
        "indexes": {
            "components_by_ref": indexes["components_by_ref"],
            "component_nets": indexes["component_nets"],
            "net_pin_count": indexes["net_pin_count"],
        },
        "diagnostics": {
            "duplicate_component_refs": indexes["duplicate_component_refs"],
            "wire_duplicates_removed": 0,
            "via_duplicates_removed": 0,
        },
    }
    return payload


def normalize_dsn(path: Path) -> Dict[str, Any]:
    """Parse DSN or read normalized JSON."""
    if path.suffix.lower() == ".json":
        return normalize_dsn_text(path.read_text(encoding="utf-8", errors="replace"), str(path))

    if path.suffix.lower() == ".dsn":
        return normalize_dsn_text(path.read_text(encoding="utf-8", errors="replace"), str(path))

    raise ValueError(f"{path}: unsupported input extension, expected .dsn or .json")


