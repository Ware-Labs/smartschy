from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .retrieval import HybridRetriever, RetrievalResult
from .utils import write_json


def _collect_relevant_schematic_pages(retrieval: RetrievalResult) -> list[int]:
    pages: set[int] = set()
    for item in retrieval.schematic_evidence:
        payload = item.payload or {}
        page_start = payload.get("page_start")
        page_end = payload.get("page_end")
        if isinstance(page_start, int):
            pages.add(page_start)
        if isinstance(page_end, int):
            pages.add(page_end)
    return sorted(pages)


def build_evidence_packet(question: str, retrieval: RetrievalResult) -> dict:
    relevant_pages = _collect_relevant_schematic_pages(retrieval)
    return {
        "question": question,
        "entities": retrieval.entities,
        "net_evidence": [asdict(item) for item in retrieval.net_evidence],
        "datasheet_evidence": [asdict(item) for item in retrieval.datasheet_evidence],
        "schematic_evidence": [asdict(item) for item in retrieval.schematic_evidence],
        "relevant_schematic_pages": relevant_pages,
    }


def build_inference_prompt(packet: dict) -> str:
    return (
        "You are a senior hardware design review assistant.\n"
        "Use only the supplied evidence. If evidence is incomplete, say so.\n\n"
        "Treat entity confidence and unresolved roles as first-class signals.\n"
        "If confidence is low, avoid definitive claims.\n\n"
        "Return output with these sections:\n"
        "1) Verdict: one sentence\n"
        "2) Reasoning: 3-6 bullet points tied to evidence\n"
        "3) Citations: bullet list of evidence IDs used\n"
        "4) Uncertainty: explicit missing information, low-confidence entities, or unresolved roles\n\n"
        "Evidence packet (JSON):\n"
        f"{packet}\n"
    )


def answer_question(
    project_root: Path,
    question: str,
    net_walk_depth: int = 1,
    top_k: int = 6,
    resolver_mode: str = "config",
) -> dict:
    retriever = HybridRetriever(project_root, resolver_mode=resolver_mode)
    retrieval = retriever.retrieve(question, net_walk_depth=net_walk_depth, top_k=top_k)
    packet = build_evidence_packet(question, retrieval)
    prompt = build_inference_prompt(packet)
    relevant_schematic_pages = packet.get("relevant_schematic_pages", [])

    derived = project_root / "derived" / "qa"
    derived.mkdir(parents=True, exist_ok=True)
    write_json(derived / "last_evidence_packet.json", packet)
    (derived / "last_prompt.txt").write_text(prompt, encoding="utf-8")

    return {
        "question": question,
        "resolver_mode": resolver_mode,
        "entities": retrieval.entities,
        "net_evidence_count": len(retrieval.net_evidence),
        "datasheet_evidence_count": len(retrieval.datasheet_evidence),
        "schematic_evidence_count": len(retrieval.schematic_evidence),
        "relevant_schematic_pages": relevant_schematic_pages,
        "evidence_packet_path": str(derived / "last_evidence_packet.json"),
        "prompt_path": str(derived / "last_prompt.txt"),
    }
