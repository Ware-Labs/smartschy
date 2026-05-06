from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from dotenv import load_dotenv

try:
    from openai import OpenAI, OpenAIError
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]
    OpenAIError = Exception  # type: ignore[assignment]

from . import evidence_tools
from .evidence_packet import build_evidence_packet, write_evidence_packet
from .obligations import derive_obligations, evaluate_obligations
from .prompt_render import render_and_write_prompt
from .retrieval import IntentRouter, SingleModeRetriever
from .utils import write_json, write_jsonl


@dataclass
class AgentLimits:
    max_iterations: int = 18
    max_tool_calls: int = 120
    max_chunks: int = 16
    max_schematic_images: int = 4
    max_total_evidence_items: int = 64


@dataclass
class AnswerOptions:
    answer_with_llm: bool = False
    planner_model: str = "gpt-5-mini"
    model: str = "gpt-5"
    max_schematic_images_for_answer: int = 4
    image_detail: str = "auto"


ProgressCallback = Callable[[str], None]


def _emit_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _priority_for_source(source_type: str) -> str:
    if source_type in {"net", "anomaly", "component", "pin"}:
        return "DSN"
    if source_type in {"function_block"}:
        return "schematic"
    if source_type == "datasheet":
        return "datasheet"
    if source_type == "schematic":
        return "schematic"
    return "inference"


def _confidence_for_source(source_type: str) -> str:
    if source_type in {"net", "anomaly", "component", "pin"}:
        return "exact"
    if source_type in {"schematic", "function_block"}:
        return "high"
    if source_type == "datasheet":
        return "high"
    return "low"


@dataclass
class AgentState:
    intent: str
    obligations: dict[str, Any]
    resolved_entities: dict[str, set[Any]]
    evidence_rows: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_decision_trace: list[dict[str, Any]] = field(default_factory=list)
    image_selection_trace: list[dict[str, Any]] = field(default_factory=list)
    selected_image_pages: list[int] = field(default_factory=list)
    finalized: bool = False
    finalize_payload: dict[str, Any] = field(default_factory=dict)


def _new_state(intent: str, obligations: dict[str, Any], entities: dict[str, Any]) -> AgentState:
    return AgentState(
        intent=intent,
        obligations=obligations,
        resolved_entities={
            "components": set(entities.get("refdes", [])),
            "nets": set(entities.get("nets", [])),
            "pins": set(entities.get("pins", [])),
            "datasheets": set(),
            "schematic_pages": set(),
        },
    )


def _state_resolved_entities(state: AgentState) -> dict[str, list[Any]]:
    return {key: sorted(value) for key, value in state.resolved_entities.items()}


def _append_evidence(state: AgentState, source_type: str, claim_supported: str, data: dict[str, Any], tool_call_id: str) -> None:
    if len(state.evidence_rows) >= 400:
        return
    row = {
        "type": source_type,
        "source_priority": _priority_for_source(source_type),
        "claim_supported": claim_supported,
        "data": data,
        "source": {"artifact": data.get("source_artifact") or data.get("source_artifacts", [])},
        "confidence": _confidence_for_source(source_type),
        "limitations": [],
        "tool_call_ids": [tool_call_id] if tool_call_id else [],
    }
    state.evidence_rows.append(row)


def _ingest_result_into_state(state: AgentState, tool_name: str, result: dict[str, Any], call_id: str) -> None:
    if tool_name in {"search_components"}:
        for row in result.get("matches", [])[:8]:
            refdes = str(row.get("refdes", "")).upper()
            if refdes:
                state.resolved_entities["components"].add(refdes)
                _append_evidence(state, "component", f"component search hit for {refdes}", row, call_id)
    elif tool_name in {"get_component", "get_component_pins"}:
        refdes = str(result.get("refdes", "")).upper()
        if refdes:
            state.resolved_entities["components"].add(refdes)
            _append_evidence(state, "component", f"component details for {refdes}", result, call_id)
        if tool_name == "get_component_pins":
            for pin in result.get("connected_pins", []):
                state.resolved_entities["pins"].add(f"{refdes}-{pin}")
            for net in result.get("connected_pin_nets", {}).values():
                if net:
                    state.resolved_entities["nets"].add(str(net))
    elif tool_name == "get_pin_net":
        refdes = str(result.get("refdes", "")).upper()
        pin = str(result.get("pin", ""))
        if refdes and pin:
            state.resolved_entities["pins"].add(f"{refdes}-{pin}")
            _append_evidence(state, "pin", f"pin connectivity for {refdes}-{pin}", result, call_id)
        net_name = str(result.get("net_name_canonical") or "")
        if net_name:
            state.resolved_entities["nets"].add(net_name)
    elif tool_name in {"search_nets"}:
        for row in result.get("matches", [])[:8]:
            net_name = str(row.get("net_name_canonical", ""))
            if net_name:
                state.resolved_entities["nets"].add(net_name)
                _append_evidence(state, "net", f"net search hit for {net_name}", row, call_id)
    elif tool_name in {"get_net", "get_net_members"}:
        payload = result.get("net", result)
        net_name = str(payload.get("net_name_canonical", result.get("net_name_canonical", "")))
        if net_name:
            state.resolved_entities["nets"].add(net_name)
            _append_evidence(state, "net", f"net evidence for {net_name}", payload, call_id)
    elif tool_name == "trace_net_neighborhood":
        _append_evidence(state, "inference", "net neighborhood trace", result, call_id)
        for node in result.get("nodes", []):
            node_id = str(node.get("id", ""))
            if node_id.startswith("component:"):
                state.resolved_entities["components"].add(node_id.split(":", 1)[1])
            if node_id.startswith("net:"):
                state.resolved_entities["nets"].add(node_id.split(":", 1)[1])
    elif tool_name in {"search_pdf_chunks", "search_datasheet_chunks"}:
        for row in result.get("results", [])[:12]:
            source_type = "datasheet" if row.get("source_type") == "datasheet" else "schematic"
            _append_evidence(state, source_type, f"{source_type} chunk supports query", row, call_id)
            page = row.get("page_start")
            if isinstance(page, int):
                state.resolved_entities["schematic_pages"].add(page)
            source_file = str(row.get("source_file", ""))
            if source_file and source_type == "datasheet":
                state.resolved_entities["datasheets"].add(source_file)
    elif tool_name == "get_schematic_pages":
        for page in result.get("relevant_pages", [])[:8]:
            if isinstance(page, int):
                state.resolved_entities["schematic_pages"].add(page)
        _append_evidence(state, "schematic", "candidate schematic pages", result, call_id)
    elif tool_name == "get_function_blocks":
        for row in result.get("blocks", [])[:20]:
            _append_evidence(state, "function_block", "functional block evidence", row, call_id)
    elif tool_name == "get_power_domains":
        for row in result.get("domains", [])[:16]:
            _append_evidence(state, "net", "power domain evidence", row, call_id)
    elif tool_name == "get_interface_buses":
        for row in result.get("buses", [])[:20]:
            _append_evidence(state, "net", "interface bus evidence", row, call_id)
    elif tool_name == "get_connectivity_anomalies":
        for row in result.get("results", [])[:20]:
            _append_evidence(state, "anomaly", "connectivity anomaly evidence", row, call_id)
    elif tool_name == "rank_schematic_images_for_obligations":
        selected_pages = [int(row.get("page_number")) for row in result.get("selected_pages", []) if isinstance(row.get("page_number"), int)]
        state.selected_image_pages = selected_pages
        state.image_selection_trace.append(
            {
                "tool_call_id": call_id,
                "selected_pages": selected_pages,
                "rationale": result.get("selected_pages", []),
            }
        )


class LocalEvidenceToolRuntime:
    def __init__(self, project_root: Path, state: AgentState):
        self.project_root = project_root
        self.state = state

    def tools(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "name": "list_project_summary", "description": "Get project artifact summary", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"type": "function", "name": "search_components", "description": "Search components by query text", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "component_type": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
            {"type": "function", "name": "get_component", "description": "Fetch component metadata for refdes", "parameters": {"type": "object", "properties": {"refdes": {"type": "string"}}, "required": ["refdes"], "additionalProperties": False}},
            {"type": "function", "name": "get_component_pins", "description": "Fetch pin map for component", "parameters": {"type": "object", "properties": {"refdes": {"type": "string"}}, "required": ["refdes"], "additionalProperties": False}},
            {"type": "function", "name": "get_pin_net", "description": "Lookup net for component pin", "parameters": {"type": "object", "properties": {"refdes": {"type": "string"}, "pin": {"type": "string"}}, "required": ["refdes", "pin"], "additionalProperties": False}},
            {"type": "function", "name": "search_nets", "description": "Search nets by name/alias", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
            {"type": "function", "name": "get_net_members", "description": "Get all component members on net", "parameters": {"type": "object", "properties": {"net_name": {"type": "string"}}, "required": ["net_name"], "additionalProperties": False}},
            {"type": "function", "name": "trace_net_neighborhood", "description": "Traverse graph around seed nets/components", "parameters": {"type": "object", "properties": {"seed_nets": {"type": "array", "items": {"type": "string"}}, "seed_components": {"type": "array", "items": {"type": "string"}}, "depth": {"type": "integer"}}, "additionalProperties": False}},
            {"type": "function", "name": "search_pdf_chunks", "description": "Search schematic or datasheet text chunks", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "source_type": {"type": "string"}, "refdes": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"], "additionalProperties": False}},
            {"type": "function", "name": "get_schematic_pages", "description": "Get relevant schematic pages for query/refdes/net", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "refdes": {"type": "string"}, "net": {"type": "string"}, "max_results": {"type": "integer"}}, "additionalProperties": False}},
            {"type": "function", "name": "get_function_blocks", "description": "Get functional block artifacts", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"type": "function", "name": "get_power_domains", "description": "Get power domain artifacts", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"type": "function", "name": "get_interface_buses", "description": "Get interface bus artifacts", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"type": "function", "name": "get_connectivity_anomalies", "description": "Get anomalies optionally filtered by refdes", "parameters": {"type": "object", "properties": {"severity": {"type": "string"}, "refdes": {"type": "string"}}, "additionalProperties": False}},
            {"type": "function", "name": "rank_schematic_images_for_obligations", "description": "Rank schematic pages for obligation closure", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer"}}, "additionalProperties": False}},
            {"type": "function", "name": "get_coverage_status", "description": "Evaluate obligation coverage against current evidence", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"type": "function", "name": "finalize_evidence", "description": "Finalize only when coverage is satisfied", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "additionalProperties": False}},
        ]

    def execute(self, tool_name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
        try:
            if tool_name == "list_project_summary":
                result = evidence_tools.list_project_summary(self.project_root)
            elif tool_name == "search_components":
                result = evidence_tools.search_components(self.project_root, query=str(args.get("query", "")), component_type=args.get("component_type"))
            elif tool_name == "get_component":
                result = evidence_tools.get_component(self.project_root, refdes=str(args.get("refdes", "")))
            elif tool_name == "get_component_pins":
                result = evidence_tools.get_component_pins(self.project_root, refdes=str(args.get("refdes", "")))
            elif tool_name == "get_pin_net":
                result = evidence_tools.get_pin_net(self.project_root, refdes=str(args.get("refdes", "")), pin=str(args.get("pin", "")))
            elif tool_name == "search_nets":
                result = evidence_tools.search_nets(self.project_root, query=str(args.get("query", "")))
            elif tool_name == "get_net_members":
                result = evidence_tools.get_net_members(self.project_root, net_name=str(args.get("net_name", "")))
            elif tool_name == "trace_net_neighborhood":
                result = evidence_tools.trace_net_neighborhood(
                    self.project_root,
                    seed_nets=args.get("seed_nets") or [],
                    seed_components=args.get("seed_components") or [],
                    depth=int(args.get("depth", 1)),
                )
            elif tool_name == "search_pdf_chunks":
                result = evidence_tools.search_pdf_chunks(
                    self.project_root,
                    query=str(args.get("query", "")),
                    source_type=str(args.get("source_type", "any")),
                    refdes=args.get("refdes"),
                    max_results=int(args.get("max_results", 10)),
                )
            elif tool_name == "get_schematic_pages":
                result = evidence_tools.get_schematic_pages(
                    self.project_root,
                    query=args.get("query"),
                    refdes=args.get("refdes"),
                    net=args.get("net"),
                    max_results=int(args.get("max_results", 6)),
                )
            elif tool_name == "get_function_blocks":
                result = evidence_tools.get_function_blocks(self.project_root)
            elif tool_name == "get_power_domains":
                result = evidence_tools.get_power_domains(self.project_root)
            elif tool_name == "get_interface_buses":
                result = evidence_tools.get_interface_buses(self.project_root)
            elif tool_name == "get_connectivity_anomalies":
                result = evidence_tools.get_connectivity_anomalies(self.project_root, severity=args.get("severity"), refdes=args.get("refdes"))
            elif tool_name == "rank_schematic_images_for_obligations":
                result = evidence_tools.rank_schematic_images_for_obligations(
                    self.project_root,
                    obligations=self.state.obligations,
                    evidence_so_far=self.state.evidence_rows,
                    max_results=int(args.get("max_results", 4)),
                )
            elif tool_name == "get_coverage_status":
                result = evaluate_obligations(self.state.obligations, _state_resolved_entities(self.state), self.state.evidence_rows)
            elif tool_name == "finalize_evidence":
                coverage = evaluate_obligations(self.state.obligations, _state_resolved_entities(self.state), self.state.evidence_rows)
                if not coverage.get("coverage_satisfied", False):
                    result = {"finalize_allowed": False, "reason": "coverage_incomplete", "coverage": coverage}
                else:
                    self.state.finalized = True
                    self.state.finalize_payload = {"reason": str(args.get("reason", "")), "coverage": coverage}
                    result = {"finalize_allowed": True, "coverage": coverage}
            else:
                raise KeyError(f"Unsupported tool: {tool_name}")
        except Exception as exc:
            result = {
                "error": {
                    "tool": tool_name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "arguments": args,
                }
            }
        _ingest_result_into_state(self.state, tool_name, result, call_id)
        self.state.tool_calls.append({"tool_name": tool_name, "arguments": args, "result_preview": str(result)[:1200], "call_id": call_id})
        return result


def _deterministic_fallback(runtime: LocalEvidenceToolRuntime, question: str, limits: AgentLimits) -> None:
    seed_nets = runtime.state.obligations.get("entities_required", {}).get("nets", [])[:6]
    for net in seed_nets:
        runtime.execute("get_net_members", {"net_name": net}, call_id=f"fallback_get_net_{net}")
    runtime.execute("get_function_blocks", {}, call_id="fallback_get_blocks")
    runtime.execute("get_power_domains", {}, call_id="fallback_get_power")
    runtime.execute("search_pdf_chunks", {"query": question, "source_type": "schematic", "max_results": min(8, limits.max_chunks)}, call_id="fallback_search_chunks")
    runtime.execute("rank_schematic_images_for_obligations", {"max_results": limits.max_schematic_images}, call_id="fallback_rank_images")
    runtime.execute("finalize_evidence", {"reason": "deterministic_fallback"}, call_id="fallback_finalize")


def _planner_prompt(question: str, state: AgentState, limits: AgentLimits) -> str:
    coverage = evaluate_obligations(state.obligations, _state_resolved_entities(state), state.evidence_rows)
    return json.dumps(
        {
            "goal": "Collect evidence until obligations are satisfied; then call finalize_evidence.",
            "question": question,
            "intent": state.intent,
            "obligations": state.obligations,
            "current_coverage": coverage,
            "current_resolved_entities": _state_resolved_entities(state),
            "evidence_count": len(state.evidence_rows),
            "limits": limits.__dict__,
            "rules": [
                "Prefer DSN-level connectivity tools for pin/net claims.",
                "For debug-protocol checks, ensure SWDIO/SWDCLK/RESET/GND and target power reference are all evidenced.",
                "Use rank_schematic_images_for_obligations before finalize to pick selective pages.",
                "Call finalize_evidence only when coverage is satisfied.",
            ],
        },
        ensure_ascii=True,
    )


def _run_llm_tool_loop(
    client: OpenAI,
    model: str,
    runtime: LocalEvidenceToolRuntime,
    question: str,
    limits: AgentLimits,
    progress_callback: ProgressCallback | None,
) -> None:
    tools = runtime.tools()
    prompt = _planner_prompt(question, runtime.state, limits)
    response = client.responses.create(
        model=model,
        tools=tools,
        input=[
            {"role": "system", "content": "You are an evidence-planning agent. Use tools iteratively and call finalize_evidence when obligations are satisfied."},
            {"role": "user", "content": prompt},
        ],
    )
    tool_calls = 0
    for iteration in range(1, max(1, limits.max_iterations) + 1):
        if runtime.state.finalized:
            break
        function_calls = [item for item in getattr(response, "output", []) if getattr(item, "type", "") == "function_call"]
        if not function_calls:
            break
        outputs = []
        for call in function_calls:
            if tool_calls >= limits.max_tool_calls:
                break
            call_name = str(getattr(call, "name", ""))
            raw_args = getattr(call, "arguments", "{}") or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            result = runtime.execute(call_name, args, call_id=str(getattr(call, "call_id", f"iter{iteration}_{tool_calls}")))
            runtime.state.tool_decision_trace.append(
                {
                    "iteration": iteration,
                    "tool": call_name,
                    "arguments": args,
                    "why": "llm_planned_call",
                }
            )
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id", ""),
                    "output": json.dumps(result, ensure_ascii=True),
                }
            )
            tool_calls += 1
            _emit_progress(progress_callback, f"Planner tool call {tool_calls}: {call_name}")
        if runtime.state.finalized or tool_calls >= limits.max_tool_calls:
            break
        if not outputs:
            break
        response = client.responses.create(
            model=model,
            tools=tools,
            previous_response_id=response.id,
            input=outputs,
        )


def _critical_findings_from_rows(evidence_rows: list[dict[str, Any]], intent: str) -> list[str]:
    findings: list[str] = []
    for row in evidence_rows:
        if row.get("type") != "anomaly":
            continue
        payload = row.get("data", {})
        kind = str(payload.get("kind", "anomaly"))
        refdes = str(payload.get("refdes", ""))
        net_name = str(payload.get("net_name", ""))
        severity = str(payload.get("severity", "medium"))
        label = refdes or net_name or "unknown_target"
        findings.append(f"{severity.upper()}: {kind} at {label}.")
    if intent == "system_function":
        findings.append("System-function conclusion is based on multi-artifact functional + connectivity coverage.")
    return list(dict.fromkeys(findings))


def _build_answer_input_with_images(prompt_text: str, project_root: Path, pages: list[int], image_detail: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "input_text", "text": prompt_text}]}]
    for page in pages:
        payload = evidence_tools.get_schematic_page_image(project_root, page_number=page, include_bytes=True)
        image_bytes = payload.get("image_bytes")
        if not image_bytes:
            continue
        mime = "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        items[0]["content"].append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": image_detail})
    return items


def run_evidence_agent(
    project_root: Path | str,
    question: str,
    limits: AgentLimits | None = None,
    answer_options: AnswerOptions | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    project_root = Path(project_root).resolve()
    limits = limits or AgentLimits()
    answer_options = answer_options or AnswerOptions()
    out_dir = project_root / "derived" / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)

    _emit_progress(progress_callback, f"Starting LLM-driven evidence agent for question: {question}")
    retriever = SingleModeRetriever(project_root)
    entities = retriever.parser.parse(question)
    intent = IntentRouter().classify(question)
    obligations = derive_obligations(question, entities).to_dict()
    state = _new_state(intent, obligations, entities)
    runtime = LocalEvidenceToolRuntime(project_root, state)

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    planner_used_llm = bool(api_key and OpenAI is not None)
    if planner_used_llm:
        _emit_progress(progress_callback, f"Running iterative LLM tool planner using {answer_options.planner_model}")
        try:
            client = OpenAI(api_key=api_key)
            _run_llm_tool_loop(client, answer_options.planner_model, runtime, question, limits, progress_callback)
        except OpenAIError:
            planner_used_llm = False
    if not planner_used_llm:
        _emit_progress(progress_callback, "LLM planner unavailable, using deterministic fallback planner")
        _deterministic_fallback(runtime, question, limits)

    # Final coverage check and finalize gate.
    coverage_report = evaluate_obligations(obligations, _state_resolved_entities(state), state.evidence_rows)
    if not state.finalized and coverage_report.get("coverage_satisfied", False):
        runtime.execute("finalize_evidence", {"reason": "post_loop_finalize"}, call_id="post_loop_finalize")
    elif not state.finalized:
        runtime.execute(
            "rank_schematic_images_for_obligations",
            {"max_results": limits.max_schematic_images},
            call_id="post_loop_rank_images",
        )

    # Ensure image ranking is attempted even when finalize happens first.
    if not state.selected_image_pages:
        runtime.execute(
            "rank_schematic_images_for_obligations",
            {"max_results": limits.max_schematic_images},
            call_id="post_finalize_rank_images",
        )

    evidence_rows = state.evidence_rows[: limits.max_total_evidence_items]
    resolved = _state_resolved_entities(state)
    critical = _critical_findings_from_rows(evidence_rows, intent)
    uncertainties: list[str] = []
    if not coverage_report.get("coverage_satisfied", False):
        uncertainties.append("obligation_coverage_incomplete")
    for rel in coverage_report.get("missing_obligations", {}).get("relations", []):
        uncertainties.append(f"missing_relation:{rel}")
    stop_reason = "coverage_satisfied_finalize" if state.finalized else "coverage_incomplete"

    packet = build_evidence_packet(
        project_root=project_root,
        question=question,
        selected_evidence=evidence_rows,
        agent_trace={
            "iterations": [{"iteration": i + 1, "tool_count": len([c for c in state.tool_calls if c])} for i in range(min(limits.max_iterations, 1 + len(state.tool_calls) // 2))],
            "stop_reason": stop_reason,
            "limits": limits.__dict__,
        },
        resolved_entities=resolved,
        open_uncertainties=sorted(set(uncertainties)) or ["none_explicitly_detected"],
        critical_findings=critical,
        recommended_answer_constraints=[
            "Use evidence IDs for every key claim.",
            "Do not finalize confident claims with unmet obligations.",
            "Prefer DSN exact evidence over inferred semantics.",
        ],
        limits=limits.__dict__,
        stop_reason=stop_reason,
        obligations=obligations,
        coverage_report=coverage_report,
        missing_obligations=coverage_report.get("missing_obligations", {}),
        tool_decision_trace=state.tool_decision_trace,
        image_selection_trace=state.image_selection_trace,
    )
    packet["intent"] = intent
    packet["evidence_diversity_metrics"] = {
        "distinct_nets": len(resolved["nets"]),
        "distinct_components": len(resolved["components"]),
        "distinct_blocks": len([row for row in evidence_rows if row.get("type") == "function_block"]),
    }

    packet_path = out_dir / "agent_evidence_packet.json"
    write_evidence_packet(packet_path, packet)
    prompt_path = out_dir / "agent_prompt.txt"
    prompt_text = render_and_write_prompt(packet, prompt_path)
    answer_path = out_dir / "agent_answer.txt"
    coverage_path = out_dir / "coverage_report.json"
    write_json(coverage_path, coverage_report)
    obligations_path = out_dir / "obligations.json"
    write_json(obligations_path, {"question": question, "obligations": obligations})

    llm_answer: dict[str, object] = {"answer_generated": False, "note": "LLM answer disabled."}

    if answer_options.answer_with_llm:
        if not api_key or OpenAI is None:
            raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")
        _emit_progress(progress_callback, f"Submitting LLM request using {answer_options.model}")
        try:
            client = OpenAI(api_key=api_key)
            selected_pages = state.selected_image_pages[: answer_options.max_schematic_images_for_answer]
            answer_input = _build_answer_input_with_images(
                prompt_text=prompt_text,
                project_root=project_root,
                pages=selected_pages,
                image_detail=answer_options.image_detail,
            )
            response = client.responses.create(model=answer_options.model, input=answer_input)
            text = response.output_text.strip()
        except OpenAIError as exc:
            raise SystemExit(f"OpenAI request failed for model '{answer_options.model}'. Details: {exc}") from exc
        answer_path.write_text(text, encoding="utf-8")
        llm_answer = {
            "answer_generated": True,
            "model": answer_options.model,
            "answer_path": str(answer_path),
            "attached_schematic_images": len(state.selected_image_pages[: answer_options.max_schematic_images_for_answer]),
            "answer_preview": text[:500],
        }

    trace_payload = {
        "generated_at_ms": int(time.time() * 1000),
        "stop_reason": stop_reason,
        "intent": intent,
        "limits": limits.__dict__,
        "summary": {
            "question": question,
            "resolved_entities": resolved,
            "evidence_item_count": len(evidence_rows),
            "coverage_satisfied": bool(coverage_report.get("coverage_satisfied", False)),
        },
    }
    write_json(out_dir / "agent_trace.json", trace_payload)
    write_json(
        out_dir / "agent_trace_summary.json",
        {
            "trace": trace_payload,
            "sufficiency": {"stop_reason": stop_reason, "coverage_satisfied": coverage_report.get("coverage_satisfied", False)},
        },
    )
    write_jsonl(out_dir / "agent_tool_calls.jsonl", state.tool_calls)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    _emit_progress(progress_callback, f"Finished in {elapsed_ms} ms (intent={intent}, stop_reason={stop_reason})")

    return {
        "question": question,
        "intent": intent,
        "stop_reason": stop_reason,
        "limits": limits.__dict__,
        "tool_call_count": len(state.tool_calls),
        "chunk_count": len([row for row in evidence_rows if row.get("type") in {"schematic", "datasheet"}]),
        "schematic_image_count": len(state.selected_image_pages),
        "evidence_item_count": len(evidence_rows),
        "resolved_entities": resolved,
        "open_uncertainties": sorted(set(uncertainties)) or ["none_explicitly_detected"],
        "coverage_report_path": str(coverage_path),
        "obligations_path": str(obligations_path),
        "agent_trace_path": str(out_dir / "agent_trace.json"),
        "agent_tool_calls_path": str(out_dir / "agent_tool_calls.jsonl"),
        "evidence_packet_path": str(packet_path),
        "prompt_path": str(prompt_path),
        "prompt_preview": prompt_text[:500],
        "llm_answer": llm_answer,
    }

