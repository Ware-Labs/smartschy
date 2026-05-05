from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .utils import canonical_net_name, split_pin_token, write_json, write_jsonl


@dataclass
class NetRecord:
    net_name_raw: str
    net_name_canonical: str
    aliases: list[str]
    pins_raw: list[str]
    pins: list[dict[str, str]]


def _extract_balanced_block(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"Could not find marker {marker!r}")
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    raise ValueError(f"Unbalanced parentheses near marker {marker!r}")


def parse_network_from_dsn(dsn_text: str) -> list[NetRecord]:
    network_block = _extract_balanced_block(dsn_text, "(network")
    pattern = re.compile(r"\(net\s+([^\s\)]+)\s*\(pins\s*(.*?)\)\s*\)", re.DOTALL)
    net_records: list[NetRecord] = []
    for match in pattern.finditer(network_block):
        raw_name = match.group(1).strip()
        pins_block = match.group(2).strip()
        pins_raw = [token.strip() for token in pins_block.split() if token.strip()]
        parsed_pins = []
        for token in pins_raw:
            refdes, pin = split_pin_token(token)
            parsed_pins.append({"token": token, "refdes": refdes, "pin": pin})
        canonical = canonical_net_name(raw_name)
        aliases = sorted({raw_name, canonical})
        net_records.append(
            NetRecord(
                net_name_raw=raw_name,
                net_name_canonical=canonical,
                aliases=aliases,
                pins_raw=pins_raw,
                pins=parsed_pins,
            )
        )
    return net_records


def build_component_pin_index(dsn_text: str, records: list[NetRecord]) -> dict:
    component_tokens = re.findall(r"\(component\s+([^\s\)]+)", dsn_text)
    image_to_refdes: dict[str, set[str]] = defaultdict(set)
    for token in component_tokens:
        if "_" not in token:
            continue
        image_name, refdes = token.rsplit("_", 1)
        if refdes:
            image_to_refdes[token].add(refdes.upper())
            image_to_refdes[image_name].add(refdes.upper())

    image_pins: dict[str, set[str]] = defaultdict(set)
    cursor = 0
    marker = "(image "
    while True:
        start = dsn_text.find(marker, cursor)
        if start < 0:
            break
        name_start = start + len(marker)
        name_end = name_start
        while name_end < len(dsn_text) and dsn_text[name_end] not in {" ", "\n", "\r", "\t", ")"}:
            name_end += 1
        image_name = dsn_text[name_start:name_end].strip()
        depth = 0
        end = start
        while end < len(dsn_text):
            ch = dsn_text[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        block = dsn_text[start:end]
        for pin_match in re.finditer(r"\(pin\s+[^\s\)]+\s+([^\s\)]+)", block):
            logical_pin = pin_match.group(1).strip()
            if logical_pin:
                image_pins[image_name].add(logical_pin)
        cursor = max(end, start + 1)

    connected_by_refdes: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for pin in record.pins:
            connected_by_refdes[pin["refdes"].upper()].add(pin["pin"])

    components: dict[str, dict] = {}
    for image_name, refdes_set in image_to_refdes.items():
        all_pins_for_image = image_pins.get(image_name, set())
        for refdes in sorted(refdes_set):
            connected = connected_by_refdes.get(refdes, set())
            floating = sorted(pin for pin in all_pins_for_image if pin and pin not in connected)
            candidate = {
                "refdes": refdes,
                "image_name": image_name,
                "all_pins": sorted(all_pins_for_image),
                "connected_pins": sorted(connected),
                "floating_pins": floating,
            }
            existing = components.get(refdes)
            if existing is None:
                components[refdes] = candidate
            else:
                existing_all = existing.get("all_pins", [])
                if len(candidate["all_pins"]) > len(existing_all):
                    components[refdes] = candidate

    # Keep entries for refdes observed in nets but not present in images/components.
    for refdes, connected in connected_by_refdes.items():
        if refdes not in components:
            components[refdes] = {
                "refdes": refdes,
                "image_name": "",
                "all_pins": sorted(connected),
                "connected_pins": sorted(connected),
                "floating_pins": [],
            }

    return {"components": components}


def build_net_graph(records: list[NetRecord]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def put_node(node_id: str, node_type: str, **attrs: str) -> None:
        nodes[node_id] = {"id": node_id, "type": node_type, **attrs}

    for record in records:
        net_node = f"net:{record.net_name_canonical}"
        put_node(
            net_node,
            "net",
            net_name_raw=record.net_name_raw,
            net_name_canonical=record.net_name_canonical,
        )
        component_pin_seen: set[str] = set()
        component_seen: set[str] = set()
        for pin in record.pins:
            cp_node = f"pin:{pin['refdes']}-{pin['pin']}"
            put_node(cp_node, "component_pin", refdes=pin["refdes"], pin=pin["pin"])
            edges.append({"src": cp_node, "dst": net_node, "type": "component_pin_to_net"})
            edges.append({"src": net_node, "dst": cp_node, "type": "net_to_component_pin"})
            component_pin_seen.add(cp_node)
            comp_node = f"component:{pin['refdes']}"
            if comp_node not in component_seen:
                put_node(comp_node, "component", refdes=pin["refdes"])
                component_seen.add(comp_node)
            edges.append({"src": comp_node, "dst": cp_node, "type": "component_to_pin"})
            edges.append({"src": cp_node, "dst": comp_node, "type": "pin_to_component"})
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["src"]].append(edge["dst"])
    return {"nodes": list(nodes.values()), "edges": edges, "adjacency": adjacency}


def build_dsn_indices(dsn_path: Path, output_dir: Path) -> dict[str, int]:
    dsn_text = dsn_path.read_text(encoding="utf-8", errors="replace")
    records = parse_network_from_dsn(dsn_text)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        output_dir / "nets.jsonl",
        [
            {
                "net_name_raw": record.net_name_raw,
                "net_name_canonical": record.net_name_canonical,
                "aliases": record.aliases,
                "pins_raw": record.pins_raw,
                "pins": record.pins,
            }
            for record in records
        ],
    )
    pin_to_net: dict[str, dict[str, str]] = {}
    for record in records:
        for pin in record.pins:
            pin_to_net[pin["token"]] = {
                "net_name_raw": record.net_name_raw,
                "net_name_canonical": record.net_name_canonical,
            }
    write_json(output_dir / "pin_to_net.json", pin_to_net)
    component_pin_index = build_component_pin_index(dsn_text, records)
    write_json(output_dir / "component_pin_index.json", component_pin_index)
    graph = build_net_graph(records)
    write_json(output_dir / "net_graph.json", graph)
    return {
        "net_count": len(records),
        "pin_count": len(pin_to_net),
        "component_count": len(component_pin_index.get("components", {})),
        "graph_node_count": len(graph["nodes"]),
        "graph_edge_count": len(graph["edges"]),
    }

