#!/usr/bin/env python3
"""First-pass LLM enrichment of board-debug questions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from llm_openai_client import OpenAIClientError, chat_completion


REQUIRED_HEADINGS = [
    "## Restated Question",
    "## Net And Component Mapping",
    "## Candidate Signal Or Current Paths",
    "## Pin-Level Evidence",
    "## Expected Vs Actual Mapping",
    "## Connector Pinout Validation",
    "## Related Required Nets",
    "## Verification Status",
    "## Evidence Needed For Final Answer",
    "## Assumptions And Unknowns",
    "## Recommended Follow-Up Checks",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich a user hardware-debug question using OpenAI + llm_summary.json."
    )
    parser.add_argument("--question", required=True, help="Original user question.")
    parser.add_argument("--summary", required=True, help="Path to llm_summary.json.")
    parser.add_argument("--out", required=True, help="Path to enriched markdown output.")
    parser.add_argument("--model", default="gpt-4.1-mini", help="OpenAI model name for first pass.")
    parser.add_argument("--mock", action="store_true", help="Emit deterministic local output instead of API call.")
    return parser.parse_args()


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_prompts(question: str, summary: Dict[str, Any]) -> Dict[str, str]:
    system_prompt = (
        "You are an electrical design assistant. Only use facts from summary JSON. "
        "Do not invent nets, refs, components, pin mappings, or connector pinouts."
    )
    template = "\n".join(REQUIRED_HEADINGS)
    user_prompt = (
        f"Original question:\n{question}\n\n"
        f"Summary JSON:\n{json.dumps(summary, ensure_ascii=True)}\n\n"
        "Write markdown using exactly these headings:\n"
        f"{template}\n\n"
        "Include pin-level evidence and expected-vs-actual mapping. "
        "For each stated component reference in mapping/pin evidence/pinout sections, include manufacturer and part number when available. "
        "If unavailable, explicitly state missing_manufacturer_or_part_number. "
        "Verification Status must contain a JSON fenced block with "
        "verification_status, answerability_score, missing_evidence."
    )
    return {"system": system_prompt, "user": user_prompt}


def _infer_verification(summary: Dict[str, Any]) -> Dict[str, Any]:
    focus_nets = summary.get("question", {}).get("question_focus_candidates", {}).get("nets", [])
    answerability_inputs = summary.get("answerability_inputs", {})
    actual_mapping = summary.get("actual_mapping", {})
    related_required = summary.get("related_required_nets", {})

    missing: List[str] = []
    all_numbers = True
    all_functions = True
    any_swap = False
    for net in focus_nets:
        inputs = answerability_inputs.get(net, {})
        if not inputs.get("has_pin_numbers", False):
            all_numbers = False
            missing.append(f"{net}:missing_pin_numbers")
        if not inputs.get("has_pin_functions", False):
            all_functions = False
            missing.append(f"{net}:missing_pin_functions")
        for item in inputs.get("missing_evidence", []):
            missing.append(f"{net}:{item}")
        if actual_mapping.get(net, {}).get("swap_hits"):
            any_swap = True
            missing.append(f"{net}:swap_detected")
    for req in related_required.get("missing", []):
        missing.append(f"related_required_net:{req}")

    missing = sorted(set(missing))
    if any_swap:
        status, score = "failed", 0.1
    elif focus_nets and all_numbers and all_functions and not related_required.get("missing"):
        status, score = "verified", 0.95
    elif focus_nets and all_numbers:
        status, score = "likely_correct", 0.65
    else:
        status, score = "inconclusive", 0.4 if focus_nets else 0.25
    return {"verification_status": status, "answerability_score": round(score, 2), "missing_evidence": missing}


def _mock_response(question: str, summary: Dict[str, Any]) -> str:
    q = summary.get("question", {})
    focus = q.get("question_focus_candidates", {})
    nets = focus.get("nets", [])
    refs = focus.get("refs", [])
    verification = _infer_verification(summary)
    related_required = summary.get("related_required_nets", {})
    connector_pinouts = summary.get("connector_pinouts", {})

    lines: List[str] = [
        "## Restated Question",
        f"The question asks: {question}",
        f"Primary focus nets from token matching: {', '.join(nets) if nets else 'none detected'}.",
        f"Primary focus refs from token matching: {', '.join(refs) if refs else 'none detected'}.",
        "",
        "## Net And Component Mapping",
    ]
    for net in nets[:12]:
        net_row = summary.get("net_index", {}).get(net, {})
        endpoint_refs = net_row.get("endpoint_refs", [])
        endpoint_parts = []
        for ref in endpoint_refs:
            c = summary.get("focus_component_catalog", {}).get(ref, {})
            endpoint_parts.append(
                f"{ref}(mfr={c.get('manufacturer')}, pn={c.get('part_number')})"
            )
        lines.append(
            f"- `{net}` endpoints: {', '.join(endpoint_parts) if endpoint_parts else ', '.join(endpoint_refs)}; fanout={net_row.get('fanout')}."
        )

    lines.extend(["", "## Candidate Signal Or Current Paths", "- Use neighbor nets and route hints to trace path plausibility."])
    lines.extend(["", "## Pin-Level Evidence"])
    for net in nets:
        lines.append(f"- Net `{net}`")
        roles = {r.get("ref"): r.get("role") for r in summary.get("endpoint_role_classification", {}).get(net, [])}
        for row in summary.get("pin_level_evidence", {}).get(net, []):
            lines.append(
                f"  - pin_token={row.get('pin_token')} ref={row.get('ref')} class={row.get('component_class')} "
                f"pin_number={row.get('pin_number')} pin_name={row.get('pin_name_or_pad')} "
                f"pin_function={row.get('pin_function')} manufacturer={row.get('manufacturer')} "
                f"part_number={row.get('part_number')} role={roles.get(row.get('ref'))} missing={row.get('missing_reason', [])}"
            )

    lines.extend(["", "## Expected Vs Actual Mapping"])
    for net in nets:
        exp = summary.get("expected_mapping", {}).get(net, {})
        act = summary.get("actual_mapping", {}).get(net, {})
        lines.append(f"- `{net}` expected_function={exp.get('expected_function')} status={act.get('status')} swap_hits={act.get('swap_hits', [])}")

    lines.extend(["", "## Connector Pinout Validation"])
    if connector_pinouts:
        for ref, payload in sorted(connector_pinouts.items()):
            lines.append(
                f"- Connector `{ref}` type={payload.get('component_id')} "
                f"manufacturer={payload.get('manufacturer')} part_number={payload.get('part_number')}"
            )
            for pin in payload.get("pins", []):
                lines.append(f"  - pin={pin.get('pin_number')} token={pin.get('pin_token')} net={pin.get('connected_net')} name={pin.get('pin_name')}")
    else:
        lines.append("- No connector pinout evidence present for focus endpoints.")

    lines.extend(
        [
            "",
            "## Related Required Nets",
            f"- required={related_required.get('required', [])}",
            f"- present={related_required.get('present', [])}",
            f"- missing={related_required.get('missing', [])}",
            "",
            "## Verification Status",
            "```json",
            json.dumps(verification, ensure_ascii=True, sort_keys=True),
            "```",
            "",
            "## Evidence Needed For Final Answer",
            f"- second_pass_ready={'yes' if verification['verification_status'] == 'verified' else 'no'}",
            "- decisive_evidence=pin_number_and_pin_function_consistency_across_focus_nets",
            f"- missing_evidence={verification['missing_evidence']}",
            "",
            "## Assumptions And Unknowns",
            "- Pin functions/electrical types may be unavailable in source artifacts and are explicitly reported.",
            "",
            "## Recommended Follow-Up Checks",
            "- Verify module/header pin-function mapping against symbol or datasheet.",
            "- Confirm connector orientation and debug header pin assignment.",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_headings(md: str) -> None:
    missing = [h for h in REQUIRED_HEADINGS if h not in md]
    if missing:
        raise ValueError(f"LLM output missing required sections: {missing}")


def main() -> None:
    args = parse_args()
    summary = _read_json(Path(args.summary))
    prompts = _build_prompts(args.question, summary)
    if args.mock:
        md = _mock_response(args.question, summary)
    else:
        try:
            md = chat_completion(model=args.model, system_prompt=prompts["system"], user_prompt=prompts["user"])
        except OpenAIClientError as exc:
            raise RuntimeError(f"OpenAI first-pass enrichment failed: {exc}") from exc

    _validate_headings(md)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md.strip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
