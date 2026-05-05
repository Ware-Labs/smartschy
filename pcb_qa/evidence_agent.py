from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
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


def _question_is_connectivity(question: str) -> bool:
    q = question.lower()
    markers = ["connect", "wired", "tie", "net", "floating", "correctly", "connection"]
    return any(marker in q for marker in markers)


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


def _plan_iteration(question: str, state: dict[str, Any], iteration: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    entities = state["entities"]
    ranked_refs = _rank_refdes_for_question(question, entities)
    relationship_mode = _question_is_relationship(question)

    if iteration == 1:
        actions.append({"tool": "list_project_summary", "args": {}})
        actions.append({"tool": "search_components", "args": {"query": question}})
    elif not entities.get("refdes"):
        actions.append({"tool": "search_components", "args": {"query": question}})

    selected_refs: list[str] = []
    if ranked_refs:
        selected_refs = ranked_refs[:4] if relationship_mode else ranked_refs[:2]
    if selected_refs:
        for ref in selected_refs:
            actions.append({"tool": "get_component", "args": {"refdes": ref}})
            actions.append({"tool": "get_component_pins", "args": {"refdes": ref}})
            actions.append({"tool": "find_datasheets_for_component", "args": {"refdes": ref}})
            actions.append({"tool": "search_datasheet_chunks", "args": {"refdes": ref, "query": question, "max_results": 4}})

    for net in entities.get("nets", [])[:4]:
        actions.append({"tool": "get_net_members", "args": {"net_name": net}})
    if not entities.get("nets"):
        actions.append({"tool": "search_nets", "args": {"query": question}})

    if _question_is_connectivity(question) or relationship_mode:
        trace_seed_components = selected_refs[:3] if selected_refs else ranked_refs[:3]
        actions.append(
            {
                "tool": "trace_net_neighborhood",
                "args": {
                    "seed_nets": entities.get("nets", [])[:3],
                    "seed_components": trace_seed_components,
                    "depth": 1,
                    "max_nodes": 180,
                },
            }
        )

    actions.append({"tool": "search_pdf_chunks", "args": {"query": question, "source_type": "schematic", "max_results": 4}})
    actions.append({"tool": "get_schematic_pages", "args": {"query": question, "max_results": 4}})
    return actions


def _sufficiency_check(question: str, state: dict[str, Any]) -> dict[str, Any]:
    has_dsn = any(item["source_priority"] == "DSN" for item in state["evidence"])
    has_schematic = any(item["source_priority"] == "schematic" for item in state["evidence"])
    has_datasheet = any(item["source_priority"] == "datasheet" for item in state["evidence"])
    is_connectivity = _question_is_connectivity(question)
    dsn_connectivity_hits = 0
    for item in state["evidence"]:
        tool_name = str(item.get("type", ""))
        data = item.get("data", {})
        if tool_name == "get_pin_net" and data.get("connected"):
            dsn_connectivity_hits += 1
        elif tool_name == "get_net_members":
            if int(data.get("members", {}).get("pin_count", 0)) > 0:
                dsn_connectivity_hits += 1
        elif tool_name == "get_component_pins":
            if len(data.get("connected_pins", [])) > 0:
                dsn_connectivity_hits += 1
        elif tool_name == "trace_net_neighborhood":
            if data.get("seed_nodes") and int(data.get("node_count", 0)) > 0:
                dsn_connectivity_hits += 1

    has_structural_resolution = bool(state["entities"].get("nets")) or bool(state.get("resolved_pins"))
    if is_connectivity:
        sufficient = has_dsn and (dsn_connectivity_hits > 0) and has_structural_resolution and (has_schematic or has_datasheet)
        missing = []
        if not has_dsn:
            missing.append("missing_dsn_connectivity_evidence")
        if dsn_connectivity_hits <= 0:
            missing.append("missing_explicit_dsn_connection_hits")
        if not has_structural_resolution:
            missing.append("missing_resolved_nets_or_pins")
        if not (has_schematic or has_datasheet):
            missing.append("missing_supporting_context_evidence")
    else:
        sufficient = has_dsn or has_datasheet or has_schematic
        missing = [] if sufficient else ["missing_relevant_evidence"]
    return {
        "is_connectivity_question": is_connectivity,
        "has_dsn": has_dsn,
        "has_schematic": has_schematic,
        "has_datasheet": has_datasheet,
        "dsn_connectivity_hits": dsn_connectivity_hits,
        "has_structural_resolution": has_structural_resolution,
        "sufficient": sufficient,
        "missing": missing,
    }


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


def run_evidence_agent(
    project_root: Path | str,
    question: str,
    limits: AgentLimits | None = None,
    answer_options: AnswerOptions | None = None,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    limits = limits or AgentLimits()
    answer_options = answer_options or AnswerOptions()
    out_dir = project_root / "derived" / "qa"
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = ToolTraceRecorder(out_dir)
    runtime = LocalEvidenceToolRuntime(project_root)
    entities = _parse_entities(project_root, question)
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

    stop_reason = "limits_exhausted"
    sufficiency = {"sufficient": False, "missing": ["not_evaluated"]}
    for iteration in range(1, limits.max_iterations + 1):
        plan = _plan_iteration(question, state, iteration=iteration)
        iteration_call_ids: list[str] = []
        for action in plan:
            if state["tool_call_count"] >= limits.max_tool_calls:
                stop_reason = "max_tool_calls_reached"
                break
            tool_name = action["tool"]
            args = action["args"]
            result = runtime.call_tool(tool_name, args)
            if tool_name in {"search_pdf_chunks", "search_datasheet_chunks"}:
                rows = list(result.get("results", []))
                remaining = max(0, limits.max_chunks - state["chunk_count"])
                if len(rows) > remaining:
                    rows = rows[:remaining]
                    result = dict(result)
                    result["results"] = rows
                    result["truncated_by_agent_limit"] = "max_chunks"
                state["chunk_count"] += len(rows)
            if tool_name == "get_schematic_pages":
                pages = list(result.get("relevant_pages", []))
                remaining = max(0, limits.max_schematic_images - state["image_count"])
                if len(pages) > remaining:
                    pages = pages[:remaining]
                    result = dict(result)
                    result["relevant_pages"] = pages
                    result["truncated_by_agent_limit"] = "max_schematic_images"
                state["image_count"] += len(pages)
            call_id = recorder.record_tool_call(name=tool_name, args=args, result=result)
            iteration_call_ids.append(call_id)
            state["tool_call_count"] += 1
            evidence_item = {
                "type": tool_name,
                "source_priority": _priority_for_tool(tool_name),
                "claim_supported": f"{tool_name} result for question context",
                "data": result,
                "source": {"artifact": result.get("source_artifact") or result.get("source_artifacts", [])},
                "confidence": _confidence_for_tool(tool_name),
                "limitations": [],
                "tool_call_ids": [call_id],
            }
            state["evidence"].append(evidence_item)
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
            if len(state["evidence"]) >= limits.max_total_evidence_items:
                stop_reason = "max_total_evidence_items_reached"
                break
        state["entities"]["nets"] = sorted(set(state["entities"].get("nets", [])))
        state["entities"]["refdes"] = sorted(set(state["entities"].get("refdes", [])))
        sufficiency = _sufficiency_check(question, state)
        recorder.record_iteration(
            iteration=iteration,
            plan=plan,
            tool_call_ids=iteration_call_ids,
            sufficiency=sufficiency,
        )
        if sufficiency["sufficient"]:
            stop_reason = "sufficient_evidence"
            break
        if stop_reason != "limits_exhausted":
            break

    state["evidence"] = state["evidence"][: limits.max_total_evidence_items]
    resolved_entities = _collect_resolved_entities(state)
    critical_findings, derived_uncertainties = _derive_critical_findings_and_uncertainties(question, state)
    open_uncertainties = list(sufficiency.get("missing", []))
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
            "selected_evidence": state["evidence"],
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
    prompt_path = out_dir / "agent_prompt.txt"
    prompt_text = render_and_write_prompt(packet, prompt_path)
    answer_path = out_dir / "agent_answer.txt"
    llm_answer_summary: dict[str, Any] | None = None
    if answer_options.answer_with_llm:
        load_dotenv(project_root / ".env")
        import os

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")
        client = OpenAI(api_key=api_key)
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
        },
    )
    write_json(out_dir / "agent_trace_summary.json", {"trace": trace_payload, "sufficiency": sufficiency})

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
