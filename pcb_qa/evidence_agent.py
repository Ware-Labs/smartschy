from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable

from dotenv import load_dotenv

try:
    from openai import OpenAI, OpenAIError
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]
    OpenAIError = Exception  # type: ignore[assignment]

from .evidence_packet import build_evidence_packet, write_evidence_packet
from .prompt_render import render_and_write_prompt
from .retrieval import RetrievalResult, SingleModeRetriever
from .utils import write_json, write_jsonl


@dataclass
class AgentLimits:
    max_iterations: int = 1
    max_tool_calls: int = 0
    max_chunks: int = 16
    max_schematic_images: int = 4
    max_total_evidence_items: int = 64


@dataclass
class AnswerOptions:
    answer_with_llm: bool = False
    model: str = "gpt-5"
    max_schematic_images_for_answer: int = 4
    image_detail: str = "auto"


ProgressCallback = Callable[[str], None]


def _emit_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _priority_for_source(source_type: str) -> str:
    if source_type in {"net", "anomaly", "component"}:
        return "DSN"
    if source_type in {"function_block"}:
        return "schematic"
    if source_type == "datasheet":
        return "datasheet"
    if source_type == "schematic":
        return "schematic"
    return "inference"


def _confidence_for_source(source_type: str) -> str:
    if source_type in {"net", "anomaly", "component"}:
        return "exact"
    if source_type in {"schematic", "function_block"}:
        return "high"
    if source_type == "datasheet":
        return "high"
    return "low"


def _evidence_rows(result: RetrievalResult, limits: AgentLimits) -> list[dict]:
    rows = []
    all_rows = (
        result.net_evidence
        + result.component_evidence
        + result.block_evidence
        + result.anomaly_evidence
        + result.schematic_evidence[: max(0, limits.max_chunks // 2)]
        + result.datasheet_evidence[: limits.max_chunks]
    )
    for item in all_rows[: limits.max_total_evidence_items]:
        rows.append(
            {
                "type": item.source_type,
                "source_priority": _priority_for_source(item.source_type),
                "claim_supported": f"{item.source_type} evidence for intent={result.intent}",
                "data": item.payload,
                "source": {"artifact": item.payload.get("source_artifact") or item.payload.get("source_artifacts", [])},
                "confidence": _confidence_for_source(item.source_type),
                "limitations": [],
                "tool_call_ids": [],
            }
        )
    return rows


def _resolved_entities(result: RetrievalResult, evidence_rows: list[dict]) -> dict[str, list]:
    components = set(result.entities.get("refdes", []))
    nets = set(result.entities.get("nets", []))
    pins: set[str] = set()
    datasheets: set[str] = set()
    pages: set[int] = set()
    for row in evidence_rows:
        data = row.get("data", {})
        if row.get("type") == "net":
            nets.add(str(data.get("net_name_canonical", "")))
            for token in data.get("pins_raw", []):
                if "-" in token:
                    refdes, _ = token.rsplit("-", 1)
                    components.add(refdes.upper())
                    pins.add(token.upper())
        if row.get("type") == "schematic":
            page = data.get("page_start")
            if isinstance(page, int):
                pages.add(page)
        if row.get("type") == "datasheet":
            source_file = str(data.get("source_file", ""))
            if source_file:
                datasheets.add(source_file)
    return {
        "components": sorted(item for item in components if item),
        "nets": sorted(item for item in nets if item),
        "pins": sorted(pins),
        "datasheets": sorted(datasheets),
        "schematic_pages": sorted(pages),
    }


def _critical_findings(result: RetrievalResult) -> list[str]:
    findings: list[str] = []
    for anomaly in result.anomaly_evidence:
        payload = anomaly.payload
        kind = str(payload.get("kind", "anomaly"))
        refdes = str(payload.get("refdes", ""))
        net_name = str(payload.get("net_name", ""))
        severity = str(payload.get("severity", "medium"))
        label = refdes or net_name or anomaly.source_id
        findings.append(f"{severity.upper()}: {kind} at {label}.")
    if result.intent == "system_function":
        findings.append("System-function answer derived from breadth-first block/net coverage.")
    return list(dict.fromkeys(findings))


def _quality_uncertainties(result: RetrievalResult, resolved: dict[str, list]) -> list[str]:
    out = list(result.open_uncertainties)
    if result.intent == "system_function":
        if len(resolved.get("nets", [])) < 5:
            out.append("low_net_diversity_for_system_function")
        if len(resolved.get("components", [])) < 3:
            out.append("low_component_coverage_for_system_function")
    if result.intent in {"pin_validation", "anomaly_check"} and not result.anomaly_evidence:
        out.append("no_targeted_anomaly_hits")
    return sorted(set(out))


def _stop_reason(result: RetrievalResult, resolved: dict[str, list]) -> str:
    if result.intent == "system_function":
        if len(resolved.get("nets", [])) < 5:
            return "insufficient_breadth"
    return "single_mode_complete"


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

    _emit_progress(progress_callback, f"Starting single-mode agent for question: {question}")
    retriever = SingleModeRetriever(project_root)
    result = retriever.retrieve(question=question, top_k=max(4, limits.max_chunks))
    evidence_rows = _evidence_rows(result, limits)
    resolved = _resolved_entities(result, evidence_rows)
    critical = _critical_findings(result)
    uncertainties = _quality_uncertainties(result, resolved)
    stop_reason = _stop_reason(result, resolved)

    packet = build_evidence_packet(
        project_root=project_root,
        question=question,
        selected_evidence=evidence_rows,
        agent_trace={
            "iterations": [{"iteration": 1, "plan": [{"tool": "single_mode_retrieval", "args": {"intent": result.intent}}]}],
            "stop_reason": stop_reason,
            "limits": limits.__dict__,
        },
        resolved_entities=resolved,
        open_uncertainties=uncertainties or ["none_explicitly_detected"],
        critical_findings=critical,
        recommended_answer_constraints=[
            "Use evidence IDs for every key claim.",
            "Prefer DSN exact evidence over inferred semantics.",
            "State uncertainty explicitly when evidence is incomplete.",
        ],
        limits=limits.__dict__,
        stop_reason=stop_reason,
    )
    packet["intent"] = result.intent
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
    llm_answer: dict[str, object] = {"answer_generated": False, "note": "LLM answer disabled."}

    if answer_options.answer_with_llm:
        load_dotenv(project_root / ".env")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key or OpenAI is None:
            raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")
        _emit_progress(progress_callback, f"Submitting LLM request using {answer_options.model}")
        try:
            client = OpenAI(api_key=api_key)
            response = client.responses.create(model=answer_options.model, input=prompt_text)
            text = response.output_text.strip()
        except OpenAIError as exc:
            raise SystemExit(f"OpenAI request failed for model '{answer_options.model}'. Details: {exc}") from exc
        answer_path.write_text(text, encoding="utf-8")
        llm_answer = {
            "answer_generated": True,
            "model": answer_options.model,
            "answer_path": str(answer_path),
            "attached_schematic_images": 0,
            "answer_preview": text[:500],
        }

    trace_payload = {
        "generated_at_ms": int(time.time() * 1000),
        "stop_reason": stop_reason,
        "intent": result.intent,
        "limits": limits.__dict__,
        "summary": {
            "question": question,
            "resolved_entities": resolved,
            "evidence_item_count": len(evidence_rows),
        },
    }
    write_json(out_dir / "agent_trace.json", trace_payload)
    write_json(out_dir / "agent_trace_summary.json", {"trace": trace_payload, "sufficiency": {"stop_reason": stop_reason}})
    write_jsonl(out_dir / "agent_tool_calls.jsonl", [{"mode": "single_mode", "tools": []}])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    _emit_progress(progress_callback, f"Finished in {elapsed_ms} ms (intent={result.intent}, stop_reason={stop_reason})")

    return {
        "question": question,
        "intent": result.intent,
        "stop_reason": stop_reason,
        "limits": limits.__dict__,
        "tool_call_count": 0,
        "chunk_count": min(len(result.schematic_evidence) + len(result.datasheet_evidence), limits.max_chunks),
        "schematic_image_count": 0,
        "evidence_item_count": len(evidence_rows),
        "resolved_entities": resolved,
        "open_uncertainties": uncertainties or ["none_explicitly_detected"],
        "agent_trace_path": str(out_dir / "agent_trace.json"),
        "agent_tool_calls_path": str(out_dir / "agent_tool_calls.jsonl"),
        "evidence_packet_path": str(packet_path),
        "prompt_path": str(prompt_path),
        "prompt_preview": prompt_text[:500],
        "llm_answer": llm_answer,
    }

