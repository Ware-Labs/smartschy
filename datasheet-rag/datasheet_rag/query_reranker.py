"""OpenAI-assisted reranking for query evidence groups."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from datasheet_rag.query_planner import QueryPlannerResult

RERANK_SCHEMA_NAME = "query_group_rerank"
RERANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ranked_group_ids": {"type": "array", "items": {"type": "string"}},
        "reason_codes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "group_id": {"type": "string"},
                    "reason_code": {"type": "string"},
                },
                "required": ["group_id", "reason_code"],
            },
        },
    },
    "required": ["ranked_group_ids", "reason_codes"],
}
RERANK_SYSTEM_PROMPT = """You rerank evidence groups for electronics-datasheet retrieval.
Return only JSON matching the provided schema.
Rank the groups by usefulness for answering the evidence goal.
Prefer coherent structured groups from the right evidence family.
Penalize noisy OCR, mechanical drawings, BOM tables, and application circuits when the question asks for pins, functions, or specifications.
Do not answer the question. Only rerank the groups."""


@dataclass(slots=True)
class QueryGroupRerankResult:
    """Rerank mode and ranked group IDs."""

    mode: str
    ranked_group_ids: list[str]
    reason_codes: dict[str, str]
    note: str


def rerank_query_groups(
    *,
    question: str,
    planner_result: QueryPlannerResult,
    candidate_groups: list[dict[str, object]],
) -> QueryGroupRerankResult:
    """Rerank candidate evidence groups using OpenAI when configured."""

    ranked_ids = [str(group["group_id"]) for group in candidate_groups]
    if not candidate_groups:
        return QueryGroupRerankResult(
            mode="local_fallback",
            ranked_group_ids=[],
            reason_codes={},
            note="No candidate groups were available for reranking.",
        )

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("DATASHEET_RAG_QUERY_MODEL")
    if not api_key or not model:
        return QueryGroupRerankResult(
            mode="local_fallback",
            ranked_group_ids=ranked_ids,
            reason_codes={},
            note="OpenAI reranker not configured; local group ranking used.",
        )

    try:
        payload = _rerank_query_groups_via_openai(
            question=question,
            planner_result=planner_result,
            candidate_groups=candidate_groups,
            api_key=api_key,
            model=model,
        )
        validated_ids, reason_codes = _validate_rerank_payload(payload, candidate_groups=candidate_groups)
        return QueryGroupRerankResult(
            mode="openai",
            ranked_group_ids=validated_ids,
            reason_codes=reason_codes,
            note="OpenAI group reranker used.",
        )
    except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
        return QueryGroupRerankResult(
            mode="local_fallback",
            ranked_group_ids=ranked_ids,
            reason_codes={},
            note=f"OpenAI group reranker failed ({type(exc).__name__}: {exc}); local group ranking used.",
        )


def _rerank_query_groups_via_openai(
    *,
    question: str,
    planner_result: QueryPlannerResult,
    candidate_groups: list[dict[str, object]],
    api_key: str,
    model: str,
) -> dict[str, object]:
    """Use the Responses API to rerank compact group summaries."""

    from openai import OpenAI

    compact_groups = []
    for group in candidate_groups[:8]:
        compact_groups.append(
            {
                "group_id": group["group_id"],
                "evidence_family": group["evidence_family"],
                "quality_score": group["quality_score"],
                "local_score": group["local_score"],
                "section_title": group.get("section_title"),
                "table_title": group.get("table_title"),
                "summary": group.get("summary"),
                "sample_texts": [text[:160] for text in group.get("sample_texts", [])[:2]],
            }
        )

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": RERANK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "intent": planner_result.spec.intent,
                        "preferred_evidence_families": planner_result.spec.preferred_evidence_families,
                        "subquestions": planner_result.spec.subquestions,
                        "evidence_goal": planner_result.spec.evidence_goal,
                        "groups": compact_groups,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": RERANK_SCHEMA_NAME,
                "strict": True,
                "schema": RERANK_SCHEMA,
            }
        },
        max_output_tokens=800,
    )
    raw_text = _extract_response_text(response)
    return json.loads(raw_text)


def _extract_response_text(response: object) -> str:
    """Extract text content from an OpenAI response object."""

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    for output_item in getattr(response, "output", []) or []:
        if getattr(output_item, "type", None) != "message":
            continue
        for content_item in getattr(output_item, "content", []) or []:
            if getattr(content_item, "type", None) == "output_text":
                text = getattr(content_item, "text", "")
                if text:
                    return text
            if getattr(content_item, "type", None) == "refusal":
                refusal = getattr(content_item, "refusal", "")
                raise RuntimeError(f"Reranker refusal: {refusal}")
    raise RuntimeError("Reranker returned no usable text output.")


def _validate_rerank_payload(
    payload: dict[str, object],
    *,
    candidate_groups: list[dict[str, object]],
) -> tuple[list[str], dict[str, str]]:
    """Validate reranker output and clamp it to known group IDs."""

    if not isinstance(payload, dict):
        raise ValueError("Reranker payload must be a JSON object.")
    group_ids = {str(group["group_id"]) for group in candidate_groups}
    ranked_group_ids = payload.get("ranked_group_ids")
    reason_codes_payload = payload.get("reason_codes")
    if not isinstance(ranked_group_ids, list):
        raise ValueError("Reranker field 'ranked_group_ids' must be an array.")
    if not isinstance(reason_codes_payload, list):
        raise ValueError("Reranker field 'reason_codes' must be an array.")

    ranked: list[str] = []
    for value in ranked_group_ids:
        if not isinstance(value, str):
            raise ValueError("Reranker ranked group IDs must be strings.")
        if value in group_ids and value not in ranked:
            ranked.append(value)
    for group in candidate_groups:
        group_id = str(group["group_id"])
        if group_id not in ranked:
            ranked.append(group_id)

    reason_codes: dict[str, str] = {}
    for item in reason_codes_payload:
        if not isinstance(item, dict):
            raise ValueError("Reranker reason codes must be objects.")
        group_id = item.get("group_id")
        reason_code = item.get("reason_code")
        if isinstance(group_id, str) and isinstance(reason_code, str) and group_id in group_ids:
            reason_codes[group_id] = reason_code.strip()
    return ranked, reason_codes
