from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .retrieval import HybridRetriever, RetrievalResult
from .utils import write_json


def build_evidence_packet(question: str, retrieval: RetrievalResult) -> dict:
    return {
        "question": question,
        "entities": retrieval.entities,
        "net_evidence": [asdict(item) for item in retrieval.net_evidence],
        "datasheet_evidence": [asdict(item) for item in retrieval.datasheet_evidence],
        "schematic_evidence": [asdict(item) for item in retrieval.schematic_evidence],
    }


def build_inference_prompt(packet: dict) -> str:
    return (
        "You are a senior hardware design review assistant.\n"
        "Use only the supplied evidence. If evidence is incomplete, say so.\n\n"
        "Return output with these sections:\n"
        "1) Verdict: one sentence\n"
        "2) Reasoning: 3-6 bullet points tied to evidence\n"
        "3) Citations: bullet list of evidence IDs used\n"
        "4) Uncertainty: explicit missing information, if any\n\n"
        "Evidence packet (JSON):\n"
        f"{packet}\n"
    )


def answer_question(
    project_root: Path,
    question: str,
    net_walk_depth: int = 1,
    top_k: int = 6,
) -> dict:
    retriever = HybridRetriever(project_root)
    retrieval = retriever.retrieve(question, net_walk_depth=net_walk_depth, top_k=top_k)
    packet = build_evidence_packet(question, retrieval)
    prompt = build_inference_prompt(packet)

    derived = project_root / "derived" / "qa"
    derived.mkdir(parents=True, exist_ok=True)
    write_json(derived / "last_evidence_packet.json", packet)
    (derived / "last_prompt.txt").write_text(prompt, encoding="utf-8")

    return {
        "question": question,
        "entities": retrieval.entities,
        "net_evidence_count": len(retrieval.net_evidence),
        "datasheet_evidence_count": len(retrieval.datasheet_evidence),
        "schematic_evidence_count": len(retrieval.schematic_evidence),
        "evidence_packet_path": str(derived / "last_evidence_packet.json"),
        "prompt_path": str(derived / "last_prompt.txt"),
    }
