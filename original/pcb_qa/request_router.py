from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .model_policy import ROUTER_MODEL_DEFAULT
from .utils import read_json

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


RouteType = str


@dataclass
class RouteDecision:
    route: RouteType
    confidence: str
    rationale: str
    model_used: str
    forced_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_router_context(project_root: Path) -> dict[str, Any]:
    ingest_summary_path = project_root / "derived" / "ingest_summary.json"
    summary = read_json(ingest_summary_path) if ingest_summary_path.exists() else {}
    return {"ingest_summary": summary}


def _heuristic_route(question: str) -> RouteDecision:
    q = question.lower()
    precision_signals = (
        "pin",
        "net",
        "value of",
        "resistor",
        "capacitance",
        "bypass",
        "voltage across",
        "connected correctly",
        "check all pins",
        "equation",
    )
    unrelated_signals = (
        "weather",
        "distance between",
        "dogs bark",
        "cats meow",
        "shopping list",
        "egg mcmuffin",
    )
    if any(token in q for token in unrelated_signals):
        return RouteDecision(
            route="irrelevant_general",
            confidence="high",
            rationale="Heuristic matched clearly off-circuit request patterns.",
            model_used="heuristic",
        )
    if any(token in q for token in precision_signals):
        return RouteDecision(
            route="precision",
            confidence="medium",
            rationale="Heuristic matched precision-style electrical analysis terms.",
            model_used="heuristic",
        )
    return RouteDecision(
        route="relevant_general",
        confidence="medium",
        rationale="Defaulted to relevant-general because precision/off-topic signals were weak.",
        model_used="heuristic",
    )


def route_request(
    project_root: Path | str,
    question: str,
    *,
    forced_mode: str | None = None,
    model: str = ROUTER_MODEL_DEFAULT,
) -> RouteDecision:
    project_root = Path(project_root).resolve()
    normalized_forced = (forced_mode or "").strip().lower() or None
    if normalized_forced in {"general", "precision"}:
        route = "relevant_general" if normalized_forced == "general" else "precision"
        return RouteDecision(
            route=route,
            confidence="forced",
            rationale=f"Route forced by caller mode='{normalized_forced}'.",
            model_used="forced_mode",
            forced_mode=normalized_forced,
        )

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    context = _load_router_context(project_root)
    if not api_key or OpenAI is None:
        return _heuristic_route(question)

    prompt = {
        "task": "Classify user question into one route.",
        "routes": ["relevant_general", "precision", "irrelevant_general"],
        "definitions": {
            "relevant_general": "Circuit-related conceptual or broad design questions.",
            "precision": "Specific calculations/pin-net verification requiring deterministic lookup.",
            "irrelevant_general": "Not meaningfully about this circuit/resources.",
        },
        "question": question,
        "project_context": context,
        "output_schema": {
            "route": "relevant_general|precision|irrelevant_general",
            "confidence": "high|medium|low",
            "rationale": "short string",
        },
    }
    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=model, input=json.dumps(prompt, ensure_ascii=True))
        raw = response.output_text.strip()
        payload = json.loads(raw) if raw else {}
        route = str(payload.get("route", "")).strip()
        confidence = str(payload.get("confidence", "low")).strip().lower()
        rationale = str(payload.get("rationale", "")).strip() or "Router did not provide rationale."
        if route not in {"relevant_general", "precision", "irrelevant_general"}:
            return _heuristic_route(question)
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        return RouteDecision(
            route=route,
            confidence=confidence,
            rationale=rationale,
            model_used=model,
            forced_mode=normalized_forced,
        )
    except Exception:
        return _heuristic_route(question)

