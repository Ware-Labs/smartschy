#!/usr/bin/env python3
"""Attempt a final answer from the latest enriched question markdown."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm_openai_client import OpenAIClientError, chat_completion


DEFAULT_SEARCH_ROOT = Path("llm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an attempted answer from an enriched_question markdown file."
    )
    parser.add_argument(
        "--enriched",
        default="",
        help="Path to enriched question markdown. If omitted, auto-picks latest llm/**/enriched_question.md",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output markdown path. Default: sibling file named attempted_answer.md",
    )
    parser.add_argument(
        "--model",
        default="gpt-4.1-mini",
        help="OpenAI model used for answer attempt.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic local output instead of OpenAI.",
    )
    return parser.parse_args()


def _find_latest_enriched(search_root: Path) -> Path:
    candidates = [
        p
        for p in search_root.rglob("enriched_question.md")
        if p.is_file()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No enriched_question.md found under: {search_root}"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _extract_section(markdown: str, heading: str) -> str:
    pattern = (
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n"
        rf"(.*?)(?=^##\s+|\Z)"
    )
    match = re.search(pattern, markdown)
    return match.group(1).strip() if match else ""


def _extract_question(markdown: str) -> str:
    section = _extract_section(markdown, "Restated Question")
    for line in section.splitlines():
        line = line.strip()
        if line.lower().startswith("the question asks:"):
            return line.split(":", 1)[1].strip()
    return ""


def _extract_verification_json(markdown: str) -> Dict[str, Any]:
    section = _extract_section(markdown, "Verification Status")
    match = re.search(r"```json\s*(\{.*?\})\s*```", section, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _collect_key_evidence(markdown: str) -> Dict[str, str]:
    return {
        "net_component_mapping": _extract_section(markdown, "Net And Component Mapping"),
        "pin_level_evidence": _extract_section(markdown, "Pin-Level Evidence"),
        "expected_vs_actual": _extract_section(markdown, "Expected Vs Actual Mapping"),
        "connector_pinout_validation": _extract_section(markdown, "Connector Pinout Validation"),
        "related_required_nets": _extract_section(markdown, "Related Required Nets"),
        "evidence_needed": _extract_section(markdown, "Evidence Needed For Final Answer"),
        "assumptions_unknowns": _extract_section(markdown, "Assumptions And Unknowns"),
    }


def _build_prompts(question: str, evidence: Dict[str, str], verification: Dict[str, Any]) -> Tuple[str, str]:
    system_prompt = (
        "You are an electrical design review assistant. "
        "Use only facts provided in the enriched markdown evidence. "
        "Do not invent component pins, mappings, or test outcomes."
    )
    user_prompt = (
        f"Original question:\n{question or 'unknown'}\n\n"
        f"Verification status object:\n{json.dumps(verification, ensure_ascii=True, sort_keys=True)}\n\n"
        "Evidence sections:\n"
        f"- Net And Component Mapping:\n{evidence['net_component_mapping']}\n\n"
        f"- Pin-Level Evidence:\n{evidence['pin_level_evidence']}\n\n"
        f"- Expected Vs Actual Mapping:\n{evidence['expected_vs_actual']}\n\n"
        f"- Connector Pinout Validation:\n{evidence['connector_pinout_validation']}\n\n"
        f"- Related Required Nets:\n{evidence['related_required_nets']}\n\n"
        f"- Evidence Needed For Final Answer:\n{evidence['evidence_needed']}\n\n"
        f"- Assumptions And Unknowns:\n{evidence['assumptions_unknowns']}\n\n"
        "Return markdown with exactly these headings:\n"
        "## Attempted Answer\n"
        "## Confidence\n"
        "## Evidence Used\n"
        "## Gaps And Follow-Up Needed\n\n"
        "In Attempted Answer, answer directly but qualify uncertainties from missing evidence. "
        "In Confidence, include a numeric confidence 0..1 and short rationale."
    )
    return system_prompt, user_prompt


def _mock_answer(question: str, verification: Dict[str, Any], evidence: Dict[str, str]) -> str:
    status = str(verification.get("verification_status", "inconclusive"))
    score = float(verification.get("answerability_score", 0.4))
    missing = verification.get("missing_evidence", [])
    if not isinstance(missing, list):
        missing = []

    if status in ("verified", "likely_correct"):
        direct = (
            "Based on the available enriched evidence, the targeted connections appear correct for the asked nets, "
            "but this is not fully definitive without the missing pin-function corroboration."
        )
    elif status == "failed":
        direct = (
            "The current evidence suggests the asked connections are likely incorrect or swapped, "
            "so the design should be treated as failing this check until verified on schematic/pin-function data."
        )
    else:
        direct = (
            "The current evidence is insufficient to conclude whether the asked connections are fully correct."
        )

    gap_lines = "\n".join(f"- {item}" for item in missing) if missing else "- No explicit missing evidence listed."
    return (
        "## Attempted Answer\n"
        f"{question or 'The question'}: {direct}\n\n"
        "## Confidence\n"
        f"- score={round(score, 2)}\n"
        f"- status={status}\n"
        "- rationale=derived from enriched verification status and available pin/net mapping\n\n"
        "## Evidence Used\n"
        "- Net/component endpoint mappings from enriched question output\n"
        "- Pin-level token evidence and connector pinout section\n"
        "- Expected-vs-actual mapping status and related required nets\n\n"
        "## Gaps And Follow-Up Needed\n"
        f"{gap_lines}\n"
    )


def _validate_output(md: str) -> None:
    required = (
        "## Attempted Answer",
        "## Confidence",
        "## Evidence Used",
        "## Gaps And Follow-Up Needed",
    )
    missing = [h for h in required if h not in md]
    if missing:
        raise ValueError(f"Answer output missing required headings: {missing}")


def main() -> None:
    args = parse_args()
    enriched_path = Path(args.enriched) if args.enriched else _find_latest_enriched(DEFAULT_SEARCH_ROOT)
    markdown = enriched_path.read_text(encoding="utf-8")

    question = _extract_question(markdown)
    verification = _extract_verification_json(markdown)
    evidence = _collect_key_evidence(markdown)

    if args.mock:
        answer_md = _mock_answer(question, verification, evidence)
    else:
        system_prompt, user_prompt = _build_prompts(question, evidence, verification)
        try:
            answer_md = chat_completion(
                model=args.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1400,
            )
        except OpenAIClientError as exc:
            raise RuntimeError(f"OpenAI answer generation failed: {exc}") from exc

    _validate_output(answer_md)
    out_path = Path(args.out) if args.out else enriched_path.with_name("attempted_answer.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(answer_md.strip() + "\n", encoding="utf-8")
    print(f"Wrote attempted answer: {out_path}")


if __name__ == "__main__":
    main()
