from __future__ import annotations

from pathlib import Path
from typing import Any


def render_prompt_from_evidence_packet(packet: dict[str, Any]) -> str:
    critical_findings = packet.get("critical_findings", []) or []
    intent = str(packet.get("intent", "system_function"))
    diversity = packet.get("evidence_diversity_metrics", {}) or {}
    hypotheses = packet.get("functional_hypotheses", []) or []
    anomaly_findings = packet.get("anomaly_findings", []) or []
    coverage = packet.get("coverage_report", {}) or {}
    missing_obligations = packet.get("missing_obligations", {}) or {}
    critical_block = ""
    if critical_findings:
        lines = "\n".join(f"- {item}" for item in critical_findings)
        critical_block = f"Critical DSN Findings:\n{lines}\n\n"
    hypothesis_block = ""
    if hypotheses:
        lines = "\n".join(f"- {item}" for item in hypotheses)
        hypothesis_block = f"Functional Hypotheses:\n{lines}\n\n"
    anomaly_block = ""
    if anomaly_findings:
        lines = "\n".join(f"- {item}" for item in anomaly_findings)
        anomaly_block = f"Anomaly Findings:\n{lines}\n\n"

    return (
        "You are a senior hardware design review assistant.\n"
        "Use only the supplied evidence packet. If evidence is incomplete, say so explicitly.\n\n"
        f"Question intent: {intent}\n"
        f"Evidence diversity metrics: {diversity}\n\n"
        f"Coverage report: {coverage}\n"
        f"Missing obligations: {missing_obligations}\n\n"
        "Evidence priority order: DSN connectivity > schematic text/image > datasheet text > BOM metadata > inferred heuristics.\n"
        "Never invent values, pins, or nets.\n"
        "If DSN confirms connectivity but pin-function evidence is missing, state that limitation.\n"
        "If any required pin is floating or unresolved in DSN evidence, call it out explicitly.\n\n"
        "If coverage_satisfied is false, the verdict must explicitly explain missing obligations before any tentative conclusion.\n\n"
        f"{critical_block}"
        f"{hypothesis_block}"
        f"{anomaly_block}"
        "Required answer format:\n"
        "1) Verdict: 2-4 sentences, may be expressive and descriptive, but must stay grounded in supplied evidence\n"
        "2) Reasoning: 3-6 bullet points tied to evidence IDs\n"
        "3) Citations: bullet list of evidence IDs used\n"
        "4) Uncertainty: explicit missing info, low-confidence evidence, unresolved entities\n\n"
        "Evidence packet JSON follows:\n"
        f"{packet}\n"
    )


def write_prompt(path: Path, prompt_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt_text, encoding="utf-8")


def render_and_write_prompt(packet: dict[str, Any], output_path: Path) -> str:
    prompt = render_prompt_from_evidence_packet(packet)
    write_prompt(output_path, prompt)
    return prompt
