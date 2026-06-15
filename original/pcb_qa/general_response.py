from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import evidence_tools
from .model_policy import GENERAL_MODEL_DEFAULT
from .request_router import RouteDecision

try:
    from openai import OpenAI, OpenAIError
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]
    OpenAIError = Exception  # type: ignore[assignment]


def _read_bom_markdown(project_root: Path) -> str:
    path = project_root / "derived" / "bom" / "bom_overview.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _load_page_images(project_root: Path, max_images: int = 6) -> list[dict[str, Any]]:
    payload = evidence_tools.get_schematic_pages(project_root, max_results=max_images)
    pages = payload.get("available_pages", [])[:max_images]
    rows: list[dict[str, Any]] = []
    for page in pages:
        try:
            image = evidence_tools.get_schematic_page_image(project_root, page_number=int(page), include_bytes=True)
        except Exception:
            continue
        image_bytes = image.get("image_bytes")
        if not image_bytes:
            continue
        encoded = base64.b64encode(image_bytes).decode("ascii")
        rows.append({"page": int(page), "image_url": f"data:image/png;base64,{encoded}"})
    return rows


def _fallback_answer(question: str, route: str) -> str:
    if route == "irrelevant_general":
        return (
            "This question appears outside the current circuit context. I can still provide a general best-effort answer, "
            "but confidence may be limited without domain-specific references from the project artifacts.\n\n"
            f"Question: {question}"
        )
    return (
        "I could not run the general LLM responder, so here is a deterministic fallback:\n"
        "- The repository contains board connectivity, BOM metadata, schematic chunks/images, and derived semantic artifacts.\n"
        "- For detailed pin/net verification, switch to precision mode.\n"
        "- For broad behavior questions, include the target subsystem or component names."
    )


def run_general_response(
    project_root: Path | str,
    question: str,
    route_decision: RouteDecision,
    *,
    model: str = GENERAL_MODEL_DEFAULT,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    circuit_summary = evidence_tools.get_circuit_summary(project_root).get("summary_markdown", "")
    bom_md = _read_bom_markdown(project_root)
    page_images = _load_page_images(project_root, max_images=4)

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        answer = _fallback_answer(question, route_decision.route)
        return {
            "mode": "general",
            "route": route_decision.to_dict(),
            "model": "fallback",
            "answer_text": answer,
            "confidence_note": "LLM unavailable; fallback response generated.",
            "suggested_precision_followup": "If needed, switch to precision mode and ask for pin/net-level verification.",
        }

    system_prompt = (
        "You are a senior PCB design assistant. Answer with practical circuit reasoning.\n"
        "If route is irrelevant_general, answer generally and state confidence limits.\n"
        "If route is relevant_general, answer from provided circuit context and suggest one precision follow-up question."
    )
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "route": route_decision.to_dict(),
                    "question": question,
                    "circuit_summary_markdown": circuit_summary[:30000],
                    "bom_markdown": bom_md[:30000],
                },
                ensure_ascii=True,
            ),
        }
    ]
    for image in page_images:
        content.append({"type": "input_image", "image_url": image["image_url"], "detail": "low"})

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        )
        text = response.output_text.strip()
    except OpenAIError as exc:
        text = _fallback_answer(question, route_decision.route) + f"\n\nModel error: {exc}"

    precision_followup = (
        "Precision follow-up: verify exact pin/net connectivity and relevant datasheet equations for the components involved."
    )
    if route_decision.route == "irrelevant_general":
        precision_followup = ""

    return {
        "mode": "general",
        "route": route_decision.to_dict(),
        "model": model,
        "answer_text": text,
        "confidence_note": (
            "Confidence may be limited if question is outside circuit context."
            if route_decision.route == "irrelevant_general"
            else "General-response confidence is based on summary artifacts and available schematic context."
        ),
        "suggested_precision_followup": precision_followup,
    }

