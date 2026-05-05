from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from . import evidence_tools
from .evidence_packet import build_evidence_packet, write_evidence_packet
from .prompt_render import render_and_write_prompt
from .retrieval import QueryParser
from .tool_trace import ToolTraceRecorder
from .utils import write_json


@dataclass
class AgentLimits:
    max_iterations: int = 6
    max_tool_calls: int = 40
    max_chunks: int = 16
    max_schematic_images: int = 4
    max_total_evidence_items: int = 64


@dataclass
class AnswerOptions:
    answer_with_llm: bool = False
    model: str = "gpt-5"
    max_schematic_images_for_answer: int = 4
    image_detail: str = "auto"


class LocalEvidenceToolRuntime:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self._tool_map: dict[str, Callable[..., dict[str, Any]]] = {
            "list_project_summary": lambda **kw: evidence_tools.list_project_summary(project_root=self.project_root),
            "search_components": lambda **kw: evidence_tools.search_components(project_root=self.project_root, **kw),
            "get_component": lambda **kw: evidence_tools.get_component(project_root=self.project_root, **kw),
            "get_component_pins": lambda **kw: evidence_tools.get_component_pins(project_root=self.project_root, **kw),
            "get_pin_net": lambda **kw: evidence_tools.get_pin_net(project_root=self.project_root, **kw),
            "search_nets": lambda **kw: evidence_tools.search_nets(project_root=self.project_root, **kw),
            "get_net": lambda **kw: evidence_tools.get_net(project_root=self.project_root, **kw),
            "get_net_members": lambda **kw: evidence_tools.get_net_members(project_root=self.project_root, **kw),
            "trace_net_neighborhood": lambda **kw: evidence_tools.trace_net_neighborhood(project_root=self.project_root, **kw),
            "search_pdf_chunks": lambda **kw: evidence_tools.search_pdf_chunks(project_root=self.project_root, **kw),
            "get_pdf_chunk": lambda **kw: evidence_tools.get_pdf_chunk(project_root=self.project_root, **kw),
            "find_datasheets_for_component": lambda **kw: evidence_tools.find_datasheets_for_component(project_root=self.project_root, **kw),
            "search_datasheet_chunks": lambda **kw: evidence_tools.search_datasheet_chunks(project_root=self.project_root, **kw),
            "get_schematic_pages": lambda **kw: evidence_tools.get_schematic_pages(project_root=self.project_root, **kw),
            "get_schematic_page_image": lambda **kw: evidence_tools.get_schematic_page_image(project_root=self.project_root, **kw),
            "get_component_context_bundle": lambda **kw: evidence_tools.get_component_context_bundle(project_root=self.project_root, **kw),
        }

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "build_evidence_packet":
            return build_evidence_packet(project_root=self.project_root, **args)
        if tool_name == "render_prompt_from_evidence_packet":
            packet = args.get("evidence_packet")
            if packet is None:
                raise ValueError("render_prompt_from_evidence_packet requires evidence_packet")
            prompt = args.get("prompt_text_override")
            if prompt is not None:
                return {"prompt_text": str(prompt)}
            return {"prompt_text": render_and_write_prompt(packet, args["output_path"])}
        fn = self._tool_map.get(tool_name)
        if fn is None:
            raise KeyError(f"Unknown tool: {tool_name}")
        return fn(**args)


ProgressCallback = Callable[[str], None]


def _emit_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback is None:
        return
    progress_callback(message)


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "list_project_summary",
            "description": "Read ingest and artifact coverage summary.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "type": "function",
            "name": "search_components",
            "description": "Search components by refdes, part number, value, or text query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "component_type": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_component",
            "description": "Get BOM and DSN pin summary for a component.",
            "parameters": {
                "type": "object",
                "properties": {"refdes": {"type": "string"}},
                "required": ["refdes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_component_pins",
            "description": "Get pin connectivity and floating pins for a component.",
            "parameters": {
                "type": "object",
                "properties": {"refdes": {"type": "string"}},
                "required": ["refdes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_pin_net",
            "description": "Get DSN net for a specific component pin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "pin": {"type": "string"},
                },
                "required": ["refdes", "pin"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_nets",
            "description": "Search DSN nets by canonical name or alias.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_net",
            "description": "Get canonical DSN net details and members.",
            "parameters": {
                "type": "object",
                "properties": {"net_name_or_alias": {"type": "string"}},
                "required": ["net_name_or_alias"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_net_members",
            "description": "Get components and pins connected to a DSN net.",
            "parameters": {
                "type": "object",
                "properties": {"net_name": {"type": "string"}},
                "required": ["net_name"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "trace_net_neighborhood",
            "description": "Trace local DSN graph around seed nets/components/pins.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seed_nets": {"type": "array", "items": {"type": "string"}},
                    "seed_pins": {"type": "array", "items": {"type": "string"}},
                    "seed_components": {"type": "array", "items": {"type": "string"}},
                    "depth": {"type": "integer"},
                    "max_nodes": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_pdf_chunks",
            "description": "Search schematic and datasheet chunks for relevant snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "source_type": {"type": "string"},
                    "refdes": {"type": "string"},
                    "mpn": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_pdf_chunk",
            "description": "Get full text and metadata for a specific chunk ID.",
            "parameters": {
                "type": "object",
                "properties": {"chunk_id": {"type": "string"}},
                "required": ["chunk_id"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "find_datasheets_for_component",
            "description": "Get likely datasheet file candidates for a component.",
            "parameters": {
                "type": "object",
                "properties": {"refdes": {"type": "string"}},
                "required": ["refdes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_datasheet_chunks",
            "description": "Search datasheet chunks for one component.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["refdes", "query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_schematic_pages",
            "description": "Get relevant schematic pages for query/refdes/net.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "refdes": {"type": "string"},
                    "net": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_schematic_page_image",
            "description": "Resolve schematic page image metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_number": {"type": "integer"},
                    "include_bytes": {"type": "boolean"},
                },
                "required": ["page_number"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "get_component_context_bundle",
            "description": "Fetch compact context bundle for one component.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refdes": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["refdes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "finalize_evidence",
            "description": (
                "Call when enough evidence is collected. Provide selected tool_call IDs, "
                "resolved entities, uncertainties, and a stop reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_tool_call_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "resolved_entities": {
                        "type": "object",
                        "properties": {
                            "components": {"type": "array", "items": {"type": "string"}},
                            "nets": {"type": "array", "items": {"type": "string"}},
                            "pins": {"type": "array", "items": {"type": "string"}},
                            "datasheets": {"type": "array", "items": {"type": "string"}},
                            "schematic_pages": {"type": "array", "items": {"type": "integer"}},
                        },
                        "additionalProperties": False,
                    },
                    "open_uncertainties": {"type": "array", "items": {"type": "string"}},
                    "stop_reason": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["selected_tool_call_ids"],
                "additionalProperties": False,
            },
        },
    ]


def _get_item_field(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _extract_function_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    output_items = _get_item_field(response, "output", []) or []
    for item in output_items:
        item_type = str(_get_item_field(item, "type", "")).lower()
        if item_type not in {"function_call", "tool_call"}:
            continue
        calls.append(
            {
                "name": str(_get_item_field(item, "name", "") or _get_item_field(item, "tool_name", "")),
                "call_id": str(_get_item_field(item, "call_id", "") or _get_item_field(item, "id", "")),
                "arguments": _parse_tool_arguments(_get_item_field(item, "arguments", {})),
            }
        )
    return calls


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_finalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    resolved_raw = payload.get("resolved_entities", {})
    resolved = resolved_raw if isinstance(resolved_raw, dict) else {}
    pages_raw = resolved.get("schematic_pages", [])
    pages: list[int] = []
    if isinstance(pages_raw, list):
        for page in pages_raw:
            try:
                pages.append(int(page))
            except Exception:
                continue
    return {
        "selected_tool_call_ids": _coerce_str_list(payload.get("selected_tool_call_ids", [])),
        "resolved_entities": {
            "components": _coerce_str_list(resolved.get("components", [])),
            "nets": _coerce_str_list(resolved.get("nets", [])),
            "pins": _coerce_str_list(resolved.get("pins", [])),
            "datasheets": _coerce_str_list(resolved.get("datasheets", [])),
            "schematic_pages": sorted(set(pages)),
        },
        "open_uncertainties": _coerce_str_list(payload.get("open_uncertainties", [])),
        "stop_reason": str(payload.get("stop_reason", "")).strip(),
        "notes": str(payload.get("notes", "")).strip(),
    }


def _orchestrator_prompt(question: str, limits: AgentLimits) -> str:
    return (
        "You are an evidence-gathering hardware QA agent. "
        "Use tools to collect DSN/BOM/schematic/datasheet evidence before finalizing. "
        "Prioritize DSN connectivity evidence for wiring questions. "
        "Do not invent facts. Only finalize once you have adequate evidence or limits force a stop.\n\n"
        f"Question: {question}\n"
        "Hard limits:\n"
        f"- max_iterations={limits.max_iterations}\n"
        f"- max_tool_calls={limits.max_tool_calls}\n"
        f"- max_chunks={limits.max_chunks}\n"
        f"- max_schematic_images={limits.max_schematic_images}\n"
        f"- max_total_evidence_items={limits.max_total_evidence_items}\n\n"
        "When done, call finalize_evidence with:\n"
        "- selected_tool_call_ids: list of call IDs that support your answer\n"
        "- resolved_entities: components/nets/pins/datasheets/schematic_pages\n"
        "- open_uncertainties: unresolved risks/unknowns\n"
        "- stop_reason and optional notes."
    )


def _question_is_relationship(question: str) -> bool:
    q = question.lower()
    markers = [
        "how does",
        "communicate",
        "interface",
        "between",
        "with the",
        "path",
        "flow",
        "through",
    ]
    return any(marker in q for marker in markers)


def _priority_for_tool(tool_name: str) -> str:
    if tool_name in {"get_pin_net", "get_net", "get_net_members", "trace_net_neighborhood", "get_component_pins"}:
        return "DSN"
    if tool_name in {"get_schematic_pages", "get_schematic_page_image"}:
        return "schematic"
    if tool_name in {"search_datasheet_chunks", "find_datasheets_for_component"}:
        return "datasheet"
    if tool_name in {"search_components", "get_component"}:
        return "BOM"
    if tool_name in {"search_pdf_chunks", "get_pdf_chunk"}:
        return "datasheet"
    return "inference"


def _confidence_for_tool(tool_name: str) -> str:
    if _priority_for_tool(tool_name) == "DSN":
        return "exact"
    if _priority_for_tool(tool_name) in {"schematic", "datasheet"}:
        return "high"
    if _priority_for_tool(tool_name) == "BOM":
        return "medium"
    return "low"


def _extract_refdes_from_text(question: str) -> set[str]:
    return {token.upper() for token in re.findall(r"\b[A-Za-z]{1,4}\d{1,3}\b", question)}


def _extract_symbol_like(question: str) -> set[str]:
    return {
        token.upper()
        for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.\-/]{1,}\b", question)
        if re.search(r"[A-Za-z]", token)
    }


def _rank_refdes_for_question(question: str, entities: dict[str, Any]) -> list[str]:
    refs = list(entities.get("refdes", []))
    if not refs:
        return []
    symbols = _extract_symbol_like(question)
    role_candidates = entities.get("role_candidates", {})
    role_scores: dict[str, float] = {}
    for rows in role_candidates.values():
        for row in rows:
            ref = str(row.get("refdes", "")).upper()
            score = float(row.get("score", 0.0))
            role_scores[ref] = max(role_scores.get(ref, 0.0), score)

    ranked_rows: list[tuple[float, str]] = []
    for ref in refs:
        ref_upper = ref.upper()
        score = role_scores.get(ref_upper, 0.0)
        if ref_upper in symbols:
            score += 4.0
        # Prefer ICs/modules/connectors as likely communication endpoints.
        alpha = "".join(ch for ch in ref_upper if ch.isalpha())
        if alpha.startswith(("U", "MOD", "J", "P")):
            score += 1.0
        ranked_rows.append((score, ref_upper))
    ranked_rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [ref for _, ref in ranked_rows]


def _select_refdes_from_search_results(question: str, matches: list[dict[str, Any]]) -> list[str]:
    symbols = _extract_symbol_like(question)
    relationship_mode = _question_is_relationship(question)
    selected: list[str] = []
    seen: set[str] = set()

    def _try_add(ref: str) -> None:
        ref_upper = ref.upper()
        if ref_upper and ref_upper not in seen:
            seen.add(ref_upper)
            selected.append(ref_upper)

    for row in matches:
        ref = str(row.get("refdes", ""))
        if not ref:
            continue
        ref_upper = ref.upper()
        part = str(row.get("part_number", "")).upper()
        value = str(row.get("value", "")).upper()
        if any(sym in ref_upper or sym in part or sym in value for sym in symbols):
            _try_add(ref_upper)

    if relationship_mode:
        for row in matches:
            if len(selected) >= 5:
                break
            ref = str(row.get("refdes", ""))
            ctype = str(row.get("component_type", ""))
            if ctype in {"ic", "connector", "header", "other"}:
                _try_add(ref)
        return selected[:5]

    for row in matches:
        if len(selected) >= 3:
            break
        _try_add(str(row.get("refdes", "")))
    return selected[:3]


def _extract_function_tokens(question: str) -> set[str]:
    upper = question.upper()
    tokens: set[str] = set()
    for key in ("VDDIO", "VDD", "VCC", "VBAT", "GND", "SCL", "SDA", "CS", "SCLK"):
        if key in upper:
            tokens.add(key)
    return tokens


def _derive_critical_findings_and_uncertainties(question: str, state: dict[str, Any]) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    uncertainties: list[str] = []
    function_tokens = _extract_function_tokens(question)
    ranked_refs = _rank_refdes_for_question(question, state.get("entities", {}))
    focus_refs = set(ranked_refs[:2]) if ranked_refs else set()

    for item in state.get("evidence", []):
        if item.get("type") != "get_component_pins":
            continue
        data = item.get("data", {})
        refdes = str(data.get("refdes", "")).upper()
        if focus_refs and refdes not in focus_refs:
            continue
        floating = sorted(str(pin) for pin in data.get("floating_pins", []))
        if floating:
            findings.append(f"DSN: {refdes} has floating pins {', '.join(floating)}.")
        if "VDDIO" in function_tokens and "5" in floating:
            findings.append(f"DSN: {refdes}-5 is floating (unconnected), contradicting expected VDDIO tie.")
            uncertainties.append(f"required_pin_floating:{refdes}-5")
        if function_tokens and not floating and not data.get("connected_pins", []):
            uncertainties.append(f"function_to_pin_unresolved:{refdes}")

    if function_tokens and not findings:
        uncertainties.append("function_to_pin_unresolved")

    # Deduplicate while preserving order.
    dedup_findings = list(dict.fromkeys(findings))
    dedup_uncertainties = list(dict.fromkeys(uncertainties))
    return dedup_findings, dedup_uncertainties


def _parse_entities(project_root: Path, question: str) -> dict[str, Any]:
    store = evidence_tools.DerivedArtifactStore(project_root)
    parser = QueryParser(
        refdes_to_part=store.refdes_to_part,
        nets=store.nets,
        project_root=project_root,
        resolver_mode="config",
    )
    entities = parser.parse(question)
    entities["refdes"] = sorted(set(entities.get("refdes", [])) | _extract_refdes_from_text(question))
    return entities


def _collect_resolved_entities(state: dict[str, Any]) -> dict[str, Any]:
    components = sorted(set(state["entities"].get("refdes", [])))
    nets = sorted(set(state["entities"].get("nets", [])))
    pins = sorted(set(state.get("resolved_pins", [])))
    datasheets = sorted(set(state.get("resolved_datasheets", [])))
    pages = sorted(set(state.get("resolved_pages", [])))
    return {
        "components": components,
        "nets": nets,
        "pins": pins,
        "datasheets": datasheets,
        "schematic_pages": pages,
    }


def _apply_result_limits(tool_name: str, result: dict[str, Any], state: dict[str, Any], limits: AgentLimits) -> dict[str, Any]:
    out = result
    if tool_name in {"search_pdf_chunks", "search_datasheet_chunks"}:
        rows = list(out.get("results", []))
        remaining = max(0, limits.max_chunks - state["chunk_count"])
        if len(rows) > remaining:
            rows = rows[:remaining]
            out = dict(out)
            out["results"] = rows
            out["truncated_by_agent_limit"] = "max_chunks"
        state["chunk_count"] += len(rows)
    if tool_name == "get_schematic_pages":
        pages = list(out.get("relevant_pages", []))
        remaining = max(0, limits.max_schematic_images - state["image_count"])
        if len(pages) > remaining:
            pages = pages[:remaining]
            out = dict(out)
            out["relevant_pages"] = pages
            out["truncated_by_agent_limit"] = "max_schematic_images"
        state["image_count"] += len(pages)
    return out


def _update_state_from_tool(question: str, tool_name: str, result: dict[str, Any], state: dict[str, Any]) -> None:
    if tool_name == "get_component_pins":
        for pin, net in result.get("connected_pin_nets", {}).items():
            state["resolved_pins"].add(f"{result['refdes']}-{pin}")
            if net:
                state["entities"].setdefault("nets", [])
                state["entities"]["nets"].append(net)
    if tool_name == "search_components":
        selected_refs = _select_refdes_from_search_results(question, list(result.get("matches", [])))
        for refdes in selected_refs:
            state["entities"].setdefault("refdes", [])
            state["entities"]["refdes"].append(refdes)
    if tool_name == "find_datasheets_for_component":
        for ds in result.get("datasheet_candidates", []):
            state["resolved_datasheets"].add(ds)
    if tool_name == "get_schematic_pages":
        for page in result.get("relevant_pages", []):
            state["resolved_pages"].add(page)
    state["entities"]["nets"] = sorted(set(state["entities"].get("nets", [])))
    state["entities"]["refdes"] = sorted(set(state["entities"].get("refdes", [])))


def run_evidence_agent(
    project_root: Path | str,
    question: str,
    limits: AgentLimits | None = None,
    answer_options: AnswerOptions | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    run_started_at = time.perf_counter()
    project_root = Path(project_root).resolve()
    limits = limits or AgentLimits()
    answer_options = answer_options or AnswerOptions()
    _emit_progress(progress_callback, f"Starting agent-ask for question: {question}")
    out_dir = project_root / "derived" / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = ToolTraceRecorder(out_dir)
    runtime = LocalEvidenceToolRuntime(project_root)
    entities = _parse_entities(project_root, question)
    _emit_progress(
        progress_callback,
        (
            "Parsed entities: "
            f"{len(entities.get('refdes', []))} components, "
            f"{len(entities.get('nets', []))} nets, "
            f"{len(entities.get('roles', []))} roles"
        ),
    )
    state: dict[str, Any] = {
        "entities": entities,
        "evidence": [],
        "resolved_pins": set(),
        "resolved_datasheets": set(),
        "resolved_pages": set(),
        "tool_call_count": 0,
        "chunk_count": 0,
        "image_count": 0,
    }
    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")
    client = OpenAI(api_key=api_key)
    tools = _tool_definitions()
    stop_reason = "max_iterations_reached"
    finalize_payload: dict[str, Any] = {
        "selected_tool_call_ids": [],
        "resolved_entities": {},
        "open_uncertainties": [],
        "stop_reason": "",
        "notes": "",
    }
    response = client.responses.create(
        model=answer_options.model,
        input=_orchestrator_prompt(question, limits),
        tools=tools,
    )
    finalized = False
    for iteration in range(1, limits.max_iterations + 1):
        _emit_progress(progress_callback, f"Iteration {iteration}/{limits.max_iterations}: waiting for model tool calls")
        turn_calls = _extract_function_calls(response)
        response_id = str(_get_item_field(response, "id", ""))
        if hasattr(recorder, "record_model_turn"):
            recorder.record_model_turn(
                iteration=iteration,
                response_id=response_id,
                finish_reason="tool_calls" if turn_calls else "no_tool_calls",
                requested_tools=[str(call.get("name", "")) for call in turn_calls],
            )
        if not turn_calls:
            stop_reason = "llm_error"
            break
        iteration_call_ids: list[str] = []
        tool_outputs: list[dict[str, Any]] = []
        for call in turn_calls:
            tool_name = str(call.get("name", "")).strip()
            call_id = str(call.get("call_id", "")).strip() or f"call_{iteration}_{len(tool_outputs) + 1}"
            args = call.get("arguments", {})
            if not isinstance(args, dict):
                args = {}
            if tool_name == "finalize_evidence":
                finalize_payload = _normalize_finalize_payload(args)
                finalized = True
                stop_reason = "model_finalize"
                _emit_progress(progress_callback, "Model requested finalize_evidence")
                break
            if state["tool_call_count"] >= limits.max_tool_calls or len(state["evidence"]) >= limits.max_total_evidence_items:
                stop_reason = "max_tool_calls_reached"
                _emit_progress(progress_callback, "Reached max tool calls limit; stopping execution")
                finalized = True
                break
            _emit_progress(
                progress_callback,
                f"Calling tool {tool_name} ({state['tool_call_count'] + 1}/{limits.max_tool_calls})",
            )
            call_started_at = time.perf_counter()
            try:
                result = runtime.call_tool(tool_name, args)
                result = _apply_result_limits(tool_name, result, state, limits)
            except Exception as exc:
                result = {"error": str(exc), "tool_name": tool_name, "args": args}
            record_id = recorder.record_tool_call(name=tool_name, args=args, result=result)
            iteration_call_ids.append(record_id)
            state["tool_call_count"] += 1
            call_elapsed_ms = int((time.perf_counter() - call_started_at) * 1000)
            _emit_progress(progress_callback, f"Completed {tool_name} in {call_elapsed_ms} ms")
            if "error" not in result:
                evidence_item = {
                    "type": tool_name,
                    "source_priority": _priority_for_tool(tool_name),
                    "claim_supported": f"{tool_name} result for question context",
                    "data": result,
                    "source": {"artifact": result.get("source_artifact") or result.get("source_artifacts", [])},
                    "confidence": _confidence_for_tool(tool_name),
                    "limitations": [],
                    "tool_call_ids": [record_id],
                }
                state["evidence"].append(evidence_item)
                _update_state_from_tool(question, tool_name, result, state)
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        {
                            "tool_call_id": record_id,
                            "tool_name": tool_name,
                            "result": result,
                        },
                        ensure_ascii=True,
                    ),
                }
            )
        recorder.record_iteration(
            iteration=iteration,
            plan=[{"tool": str(call.get("name", "")), "args": call.get("arguments", {})} for call in turn_calls],
            tool_call_ids=iteration_call_ids,
            sufficiency={"finalized": finalized, "stop_reason": stop_reason},
        )
        if finalized:
            break
        if not tool_outputs:
            stop_reason = "llm_error"
            break
        _emit_progress(progress_callback, f"Iteration {iteration}: submitted {len(tool_outputs)} tool outputs to model")
        response = client.responses.create(
            model=answer_options.model,
            previous_response_id=response_id,
            input=tool_outputs,
            tools=tools,
        )
    state["evidence"] = state["evidence"][: limits.max_total_evidence_items]
    resolved_entities = _collect_resolved_entities(state)
    finalized_entities = finalize_payload.get("resolved_entities", {})
    if isinstance(finalized_entities, dict):
        resolved_entities = {
            "components": sorted(set(resolved_entities["components"]) | set(_coerce_str_list(finalized_entities.get("components", [])))),
            "nets": sorted(set(resolved_entities["nets"]) | set(_coerce_str_list(finalized_entities.get("nets", [])))),
            "pins": sorted(set(resolved_entities["pins"]) | set(_coerce_str_list(finalized_entities.get("pins", [])))),
            "datasheets": sorted(
                set(resolved_entities["datasheets"]) | set(_coerce_str_list(finalized_entities.get("datasheets", [])))
            ),
            "schematic_pages": sorted(
                set(int(page) for page in resolved_entities["schematic_pages"])
                | set(int(page) for page in finalized_entities.get("schematic_pages", []) if str(page).isdigit())
            ),
        }
    selected_ids = set(_coerce_str_list(finalize_payload.get("selected_tool_call_ids", [])))
    selected_evidence = [
        item for item in state["evidence"] if selected_ids.intersection(set(item.get("tool_call_ids", [])))
    ]
    if not selected_evidence:
        selected_evidence = list(state["evidence"])
    critical_findings, derived_uncertainties = _derive_critical_findings_and_uncertainties(question, state)
    open_uncertainties = _coerce_str_list(finalize_payload.get("open_uncertainties", []))
    open_uncertainties.extend(derived_uncertainties)
    open_uncertainties = list(dict.fromkeys(open_uncertainties))
    if not open_uncertainties:
        open_uncertainties = ["none_explicitly_detected"]
    constraints = [
        "Use evidence IDs for every key claim.",
        "Do not infer connectivity when DSN evidence is absent.",
        "State uncertainty explicitly when evidence is incomplete.",
    ]
    packet = runtime.call_tool(
        "build_evidence_packet",
        {
            "question": question,
            "selected_evidence": selected_evidence,
            "agent_trace": {"iterations": recorder._iterations, "limits": limits.__dict__, "stop_reason": stop_reason},
            "resolved_entities": resolved_entities,
            "open_uncertainties": open_uncertainties,
            "critical_findings": critical_findings,
            "recommended_answer_constraints": constraints,
            "limits": limits.__dict__,
            "stop_reason": stop_reason,
        },
    )
    packet_path = out_dir / "agent_evidence_packet.json"
    write_evidence_packet(packet_path, packet)
    _emit_progress(progress_callback, f"Wrote evidence packet: {packet_path}")
    prompt_path = out_dir / "agent_prompt.txt"
    prompt_text = render_and_write_prompt(packet, prompt_path)
    _emit_progress(progress_callback, f"Wrote strict prompt: {prompt_path}")
    answer_path = out_dir / "agent_answer.txt"
    llm_answer_summary: dict[str, Any] | None = None
    if answer_options.answer_with_llm:
        _emit_progress(progress_callback, f"Preparing LLM answer with model {answer_options.model}")
        relevant_pages = list(packet.get("resolved_entities", {}).get("schematic_pages", []))
        multimodal_content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt_text}]
        image_count = 0
        for page_number in relevant_pages:
            if image_count >= max(0, answer_options.max_schematic_images_for_answer):
                break
            try:
                image_meta = evidence_tools.get_schematic_page_image(
                    project_root=project_root,
                    page_number=int(page_number),
                    include_bytes=False,
                )
            except Exception:
                continue
            image_path = Path(str(image_meta["image_path"]))
            if not image_path.exists():
                continue
            import base64

            image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
            multimodal_content.append(
                {"type": "input_text", "text": f"Schematic page {page_number} from relevant evidence."}
            )
            multimodal_content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{image_b64}",
                    "detail": answer_options.image_detail,
                }
            )
            image_count += 1
        _emit_progress(progress_callback, f"Submitting LLM request (attached images: {image_count})")
        try:
            if image_count > 0:
                response = client.responses.create(
                    model=answer_options.model,
                    input=[{"role": "user", "content": multimodal_content}],
                )
            else:
                response = client.responses.create(
                    model=answer_options.model,
                    input=prompt_text,
                )
        except OpenAIError as exc:
            raise SystemExit(
                f"OpenAI request failed for model '{answer_options.model}'. Details: {exc}"
            ) from exc
        answer_text = response.output_text.strip()
        answer_path.write_text(answer_text, encoding="utf-8")
        _emit_progress(progress_callback, f"Wrote final answer: {answer_path}")
        llm_answer_summary = {
            "answer_generated": True,
            "model": answer_options.model,
            "answer_path": str(answer_path),
            "attached_schematic_images": image_count,
            "answer_preview": answer_text[:500],
        }

    trace_payload = recorder.write_trace(
        limits=limits.__dict__,
        stop_reason=stop_reason,
        summary={
            "question": question,
            "resolved_entities": resolved_entities,
            "evidence_item_count": len(packet.get("evidence", [])),
            "model_finalize_notes": finalize_payload.get("notes", ""),
        },
    )
    write_json(out_dir / "agent_trace_summary.json", {"trace": trace_payload, "sufficiency": {"stop_reason": stop_reason}})
    elapsed_ms = int((time.perf_counter() - run_started_at) * 1000)
    _emit_progress(
        progress_callback,
        (
            f"Finished agent-ask in {elapsed_ms} ms "
            f"(stop_reason={stop_reason}, tool_calls={state['tool_call_count']})"
        ),
    )

    return {
        "question": question,
        "stop_reason": stop_reason,
        "limits": limits.__dict__,
        "tool_call_count": state["tool_call_count"],
        "chunk_count": state["chunk_count"],
        "schematic_image_count": state["image_count"],
        "evidence_item_count": len(packet.get("evidence", [])),
        "resolved_entities": resolved_entities,
        "open_uncertainties": open_uncertainties,
        "agent_trace_path": str(out_dir / "agent_trace.json"),
        "agent_tool_calls_path": str(out_dir / "agent_tool_calls.jsonl"),
        "evidence_packet_path": str(packet_path),
        "prompt_path": str(prompt_path),
        "prompt_preview": prompt_text[:500],
        "llm_answer": llm_answer_summary or {
            "answer_generated": False,
            "note": "Run with answer_with_llm enabled to generate final answer.",
        },
    }
