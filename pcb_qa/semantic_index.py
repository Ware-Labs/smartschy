from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import json

from .utils import canonical_net_name, read_json, read_jsonl, tokenize, write_json, write_jsonl

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None  # type: ignore[assignment]


POWER_NET_HINTS = (
    "VBUS",
    "VBAT",
    "VDD",
    "VCC",
    "1V8",
    "2V8",
    "3V3",
    "5V",
    "GND",
    "PMID",
    "VSS",
)

INTERFACE_HINTS = {
    "swd": ("SWD", "RESET"),
    "i2c": ("SCL", "SDA", "I2C"),
    "spi": ("MOSI", "MISO", "SCLK", "CS", "SPI"),
    "uart": ("UART", "TX", "RX"),
    "usb": ("USB", "VBUS", "D+", "D-", "J4"),
}

PROTOCOL_REQUIREMENT_LIBRARY = {
    "swd_jtag_debug": {
        "required_nets": ["SWDIO", "SWDCLK", "RESET", "GND"],
        "power_reference_any_of": ["1V8", "VDD", "VTREF"],
        "description": "Debug/programming interface should expose data/clock/reset, ground, and target reference voltage.",
    }
}


def _load_artifacts(project_root: Path) -> dict[str, Any]:
    derived = project_root / "derived"
    return {
        "nets": read_jsonl(derived / "dsn" / "nets.jsonl"),
        "component_pin_index": read_json(derived / "dsn" / "component_pin_index.json"),
        "refdes_to_part": read_json(derived / "bom" / "refdes_to_part.json"),
        "pdf_chunks": read_jsonl(derived / "pdf" / "pdf_chunks.jsonl"),
    }


def _component_type(refdes: str) -> str:
    alpha = "".join(ch for ch in refdes.upper() if ch.isalpha())
    if alpha.startswith("MOD"):
        return "module"
    if alpha.startswith("U"):
        return "ic"
    if alpha.startswith("J"):
        return "connector"
    if alpha.startswith("TP"):
        return "test_point"
    if alpha.startswith("R"):
        return "resistor"
    if alpha.startswith("C"):
        return "capacitor"
    if alpha.startswith("L"):
        return "inductor"
    if alpha.startswith("D"):
        return "diode"
    if alpha.startswith("SW"):
        return "switch"
    if alpha.startswith("X"):
        return "crystal"
    return "other"


def _is_power_net(net_name: str) -> bool:
    upper = canonical_net_name(net_name)
    return any(hint in upper for hint in POWER_NET_HINTS)


def _infer_power_domains(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in nets:
        name = str(row.get("net_name_canonical", ""))
        if not _is_power_net(name):
            continue
        pins = row.get("pins_raw", [])
        rows.append(
            {
                "domain_id": f"power:{name}",
                "net_name": name,
                "net_aliases": row.get("aliases", []),
                "pin_count": len(pins),
                "category": "ground" if name == "GND" else "supply",
                "confidence": "exact_dsn",
            }
        )
    rows.sort(key=lambda item: item["net_name"])
    return rows


def _infer_interface_buses(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bus_map: dict[str, dict[str, Any]] = {}
    for row in nets:
        net = str(row.get("net_name_canonical", ""))
        for bus, hints in INTERFACE_HINTS.items():
            if any(hint in net for hint in hints):
                payload = bus_map.setdefault(
                    bus,
                    {
                        "bus_id": f"bus:{bus}",
                        "bus_type": bus,
                        "nets": [],
                        "confidence": "heuristic",
                    },
                )
                payload["nets"].append(net)
    out = []
    for bus in sorted(bus_map):
        payload = bus_map[bus]
        payload["nets"] = sorted(set(payload["nets"]))
        out.append(payload)
    return out


def _infer_function_blocks(
    nets: list[dict[str, Any]],
    refdes_to_part: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_block: dict[str, dict[str, Any]] = {
        "power_path": {
            "block_id": "block:power_path",
            "label": "Power Path",
            "component_refs": [],
            "nets": [],
            "confidence": "deterministic",
        },
        "compute_control": {
            "block_id": "block:compute_control",
            "label": "Compute and Control",
            "component_refs": [],
            "nets": [],
            "confidence": "deterministic",
        },
        "io_debug": {
            "block_id": "block:io_debug",
            "label": "I/O and Debug",
            "component_refs": [],
            "nets": [],
            "confidence": "deterministic",
        },
    }
    for refdes, meta in refdes_to_part.items():
        text = " ".join(
            [
                refdes,
                str(meta.get("part_number", "")),
                str(meta.get("manufacturer", "")),
                str(meta.get("specification", "")),
                str(meta.get("value", "")),
            ]
        ).upper()
        ctype = _component_type(refdes)
        if ctype in {"ic", "module"}:
            by_block["compute_control"]["component_refs"].append(refdes.upper())
        if ctype in {"connector", "test_point", "switch"} or "SWD" in text:
            by_block["io_debug"]["component_refs"].append(refdes.upper())
        if ctype in {"inductor", "capacitor", "diode"} or "CHARG" in text or "REGUL" in text:
            by_block["power_path"]["component_refs"].append(refdes.upper())
    for net in nets:
        name = str(net.get("net_name_canonical", ""))
        upper = name.upper()
        if _is_power_net(upper):
            by_block["power_path"]["nets"].append(name)
        if upper.startswith(("P0.", "P1.", "P2.")) or upper.startswith(("SWD", "RESET")):
            by_block["io_debug"]["nets"].append(name)
        if upper.startswith(("P0.", "P1.", "P2.")) or upper.startswith(("XL",)):
            by_block["compute_control"]["nets"].append(name)
    out = []
    for key in ("power_path", "compute_control", "io_debug"):
        row = by_block[key]
        row["component_refs"] = sorted(set(row["component_refs"]))
        row["nets"] = sorted(set(row["nets"]))
        out.append(row)
    return out


def _infer_connectivity_anomalies(
    component_pin_index: dict[str, Any],
    nets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    components = component_pin_index.get("components", {})
    for refdes, payload in components.items():
        ctype = _component_type(refdes)
        floating = payload.get("floating_pins", [])
        if ctype in {"ic", "module"} and floating:
            anomalies.append(
                {
                    "anomaly_id": f"floating:{refdes}",
                    "severity": "high",
                    "kind": "floating_required_pin",
                    "refdes": refdes,
                    "pins": floating[:20],
                    "evidence": ["derived/dsn/component_pin_index.json"],
                    "confidence": "exact_dsn",
                }
            )
    for row in nets:
        net = str(row.get("net_name_canonical", ""))
        pins = list(row.get("pins_raw", []))
        unique = list(dict.fromkeys(pins))
        if len(unique) != len(pins):
            anomalies.append(
                {
                    "anomaly_id": f"duplicate_members:{net}",
                    "severity": "medium",
                    "kind": "duplicate_pin_membership",
                    "net_name": net,
                    "duplicate_count": len(pins) - len(unique),
                    "evidence": ["derived/dsn/nets.jsonl"],
                    "confidence": "exact_dsn",
                }
            )
        non_tp = [token for token in unique if not token.upper().startswith("TP")]
        if len(non_tp) <= 1 and net != "GND":
            anomalies.append(
                {
                    "anomaly_id": f"orphan:{net}",
                    "severity": "low",
                    "kind": "orphan_or_stub_net",
                    "net_name": net,
                    "pin_count": len(unique),
                    "evidence": ["derived/dsn/nets.jsonl"],
                    "confidence": "exact_dsn",
                }
            )
    return anomalies


def _build_typed_graph(
    nets: list[dict[str, Any]],
    function_blocks: list[dict[str, Any]],
    power_domains: list[dict[str, Any]],
    interface_buses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in function_blocks:
        block_id = block["block_id"]
        for refdes in block.get("component_refs", []):
            rows.append(
                {
                    "src": block_id,
                    "dst": f"component:{refdes}",
                    "relation": "contains_component",
                    "confidence": "deterministic",
                }
            )
        for net in block.get("nets", []):
            rows.append(
                {
                    "src": block_id,
                    "dst": f"net:{net}",
                    "relation": "contains_net",
                    "confidence": "deterministic",
                }
            )
    for domain in power_domains:
        net = str(domain.get("net_name", ""))
        rows.append(
            {
                "src": domain["domain_id"],
                "dst": f"net:{net}",
                "relation": "domain_net",
                "confidence": "exact_dsn",
            }
        )
    for bus in interface_buses:
        for net in bus.get("nets", []):
            rows.append(
                {
                    "src": bus["bus_id"],
                    "dst": f"net:{net}",
                    "relation": "bus_member",
                    "confidence": "heuristic",
                }
            )
    for row in nets:
        net = str(row.get("net_name_canonical", ""))
        for token in row.get("pins_raw", []):
            if "-" not in token:
                continue
            refdes, pin = token.rsplit("-", 1)
            rows.append(
                {
                    "src": f"component:{refdes.upper()}",
                    "dst": f"net:{net}",
                    "relation": "connected_to",
                    "pin": pin,
                    "confidence": "exact_dsn",
                }
            )
    return rows


def _llm_enrich_semantics(
    project_root: Path,
    function_blocks: list[dict[str, Any]],
    interface_buses: list[dict[str, Any]],
    pdf_chunks: list[dict[str, Any]],
    llm_model: str,
) -> dict[str, list[dict[str, Any]]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return {
            "block_semantics": [],
            "interface_hypotheses": [],
            "analog_classifications": [],
        }

    context_chunks = []
    for row in pdf_chunks:
        if row.get("source_type") != "schematic":
            continue
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        context_chunks.append(
            {
                "chunk_id": row.get("chunk_id", ""),
                "heading_path": row.get("heading_path", []),
                "text": text[:450],
            }
        )
        if len(context_chunks) >= 12:
            break

    prompt = {
        "task": "Summarize circuit block semantics and interface intent with conservative confidence.",
        "constraints": [
            "Do not claim exact electrical truth beyond provided deterministic data.",
            "Always include evidence refs to block_id, bus_id, and chunk_id when used.",
            "Use confidence high/medium/low.",
        ],
        "function_blocks": function_blocks,
        "interface_buses": interface_buses,
        "schematic_context": context_chunks,
        "output_schema": {
            "block_semantics": [
                {"block_id": "string", "summary": "string", "confidence": "high|medium|low", "evidence": []}
            ],
            "interface_hypotheses": [
                {"bus_id": "string", "hypothesis": "string", "confidence": "high|medium|low", "evidence": []}
            ],
            "analog_classifications": [
                {"label": "string", "summary": "string", "confidence": "high|medium|low", "evidence": []}
            ],
        },
    }

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=llm_model,
            input=json.dumps(prompt, ensure_ascii=True),
        )
        text = response.output_text.strip()
        data = json.loads(text) if text else {}
    except Exception:
        return {
            "block_semantics": [],
            "interface_hypotheses": [],
            "analog_classifications": [],
        }

    out = {
        "block_semantics": data.get("block_semantics", []) if isinstance(data, dict) else [],
        "interface_hypotheses": data.get("interface_hypotheses", []) if isinstance(data, dict) else [],
        "analog_classifications": data.get("analog_classifications", []) if isinstance(data, dict) else [],
    }
    # Keep only schema-ish rows.
    for key in list(out):
        out[key] = [row for row in out[key] if isinstance(row, dict)]
    return out


def _infer_sheet_semantics(pdf_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_page: dict[int, dict[str, Any]] = {}
    for row in pdf_chunks:
        if row.get("source_type") != "schematic":
            continue
        page = row.get("page_start")
        if not isinstance(page, int):
            continue
        payload = by_page.setdefault(
            page,
            {
                "page_number": page,
                "sheet_hints": set(),
                "tokens": set(),
                "queryability_tags": set(),
            },
        )
        for heading in row.get("heading_path", []):
            payload["sheet_hints"].add(str(heading))
        for token in row.get("tokens", [])[:300]:
            payload["tokens"].add(str(token).upper())
    out = []
    for page in sorted(by_page):
        payload = by_page[page]
        tokens = payload["tokens"]
        if any(tok.startswith("SWD") or tok.startswith("JTAG") for tok in tokens):
            payload["queryability_tags"].add("debug")
        if any(tok in {"VBUSIN", "VBAT", "1V8", "2V8_SW", "GND"} for tok in tokens):
            payload["queryability_tags"].add("power")
        if any("IMU" in tok or "ICM" in tok for tok in tokens):
            payload["queryability_tags"].add("sensor")
        if any(tok.startswith("J") and tok[1:].isdigit() for tok in tokens):
            payload["queryability_tags"].add("connector")
        out.append(
            {
                "page_number": page,
                "sheet_hints": sorted(payload["sheet_hints"]),
                "queryability_tags": sorted(payload["queryability_tags"]),
                "confidence": "heuristic",
            }
        )
    return out


def _infer_entity_sheet_index(nets: list[dict[str, Any]], pdf_chunks: list[dict[str, Any]]) -> dict[str, Any]:
    # Lightweight lexical index to map entities to likely schematic pages.
    by_page_text: dict[int, str] = {}
    for row in pdf_chunks:
        if row.get("source_type") != "schematic":
            continue
        page = row.get("page_start")
        if not isinstance(page, int):
            continue
        by_page_text[page] = (by_page_text.get(page, "") + " " + str(row.get("text", ""))).upper()
    net_to_pages: dict[str, list[int]] = {}
    for net in nets:
        net_name = str(net.get("net_name_canonical", ""))
        if not net_name:
            continue
        pages = [page for page, text in by_page_text.items() if net_name in text]
        if pages:
            net_to_pages[net_name] = sorted(set(pages))[:8]
    return {"net_to_pages": net_to_pages}


def _llm_protocol_obligations(
    llm_model: str,
    protocol_library: dict[str, Any],
    sheet_semantics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return []
    prompt = {
        "task": "Produce generalized protocol evidence obligations from deterministic protocol library and sheet tags.",
        "constraints": [
            "Return JSON array only.",
            "Do not invent protocol names beyond input library unless confidence is low.",
            "For each row include protocol_id, obligations, confidence, and evidence references.",
        ],
        "protocol_library": protocol_library,
        "sheet_semantics": sheet_semantics[:12],
    }
    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=llm_model, input=json.dumps(prompt, ensure_ascii=True))
        raw = response.output_text.strip()
        payload = json.loads(raw) if raw else []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    except Exception:
        return []
    return []


def build_semantic_indices(
    project_root: Path,
    llm_enrich: bool = False,
    llm_model: str = "gpt-5-mini",
) -> dict[str, int]:
    artifacts = _load_artifacts(project_root)
    nets = artifacts["nets"]
    component_pin_index = artifacts["component_pin_index"]
    refdes_to_part = artifacts["refdes_to_part"]
    pdf_chunks = artifacts["pdf_chunks"]

    power_domains = _infer_power_domains(nets)
    interface_buses = _infer_interface_buses(nets)
    function_blocks = _infer_function_blocks(nets, refdes_to_part)
    anomalies = _infer_connectivity_anomalies(component_pin_index, nets)
    typed_graph = _build_typed_graph(nets, function_blocks, power_domains, interface_buses)
    sheet_semantics = _infer_sheet_semantics(pdf_chunks)
    entity_sheet_index = _infer_entity_sheet_index(nets, pdf_chunks)

    kg_dir = project_root / "derived" / "kg"
    qa_dir = project_root / "derived" / "qa"
    kg_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)

    write_jsonl(kg_dir / "typed_graph.jsonl", typed_graph)
    write_json(kg_dir / "function_blocks.json", {"blocks": function_blocks})
    write_json(kg_dir / "power_domains.json", {"domains": power_domains})
    write_json(kg_dir / "interface_buses.json", {"buses": interface_buses})
    write_jsonl(qa_dir / "connectivity_anomalies.jsonl", anomalies)
    write_jsonl(kg_dir / "llm_sheet_semantics.jsonl", sheet_semantics)
    write_json(kg_dir / "llm_entity_sheet_index.json", entity_sheet_index)

    block_semantics: list[dict[str, Any]] = []
    interface_hypotheses: list[dict[str, Any]] = []
    analog_classifications: list[dict[str, Any]] = []
    if llm_enrich:
        llm_payload = _llm_enrich_semantics(
            project_root=project_root,
            function_blocks=function_blocks,
            interface_buses=interface_buses,
            pdf_chunks=pdf_chunks,
            llm_model=llm_model,
        )
        block_semantics = llm_payload["block_semantics"]
        interface_hypotheses = llm_payload["interface_hypotheses"]
        analog_classifications = llm_payload["analog_classifications"]
    protocol_obligations = _llm_protocol_obligations(
        llm_model=llm_model,
        protocol_library=PROTOCOL_REQUIREMENT_LIBRARY,
        sheet_semantics=sheet_semantics,
    ) if llm_enrich else []
    write_jsonl(kg_dir / "llm_block_semantics.jsonl", block_semantics)
    write_jsonl(kg_dir / "llm_interface_hypotheses.jsonl", interface_hypotheses)
    write_jsonl(kg_dir / "llm_analog_classification.jsonl", analog_classifications)
    write_jsonl(kg_dir / "llm_protocol_obligations.jsonl", protocol_obligations)

    return {
        "typed_graph_edges": len(typed_graph),
        "function_blocks": len(function_blocks),
        "power_domains": len(power_domains),
        "interface_buses": len(interface_buses),
        "connectivity_anomalies": len(anomalies),
        "llm_block_semantics": len(block_semantics),
        "llm_interface_hypotheses": len(interface_hypotheses),
        "llm_analog_classification": len(analog_classifications),
        "llm_sheet_semantics": len(sheet_semantics),
        "llm_protocol_obligations": len(protocol_obligations),
    }

