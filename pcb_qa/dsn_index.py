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
    graph = build_net_graph(records)
    write_json(output_dir / "net_graph.json", graph)
    return {
        "net_count": len(records),
        "pin_count": len(pin_to_net),
        "graph_node_count": len(graph["nodes"]),
        "graph_edge_count": len(graph["edges"]),
    }

