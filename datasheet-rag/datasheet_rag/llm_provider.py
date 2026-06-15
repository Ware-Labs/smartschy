"""Provider-agnostic grounded-answer generation adapters."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


@dataclass(slots=True)
class AnswerCitation:
    """One citation emitted by the grounded-answer provider."""

    page_number: int
    section_title: str | None
    table_title: str | None
    row_index: int | None
    chunk_type: str
    source_note: str | None = None


@dataclass(slots=True)
class GroundedAnswerRequest:
    """Provider-agnostic grounded-answer request."""

    question: str
    intent: str
    primary_subject: str
    retrieval_summary: str
    coverage_notes: list[str]
    evidence_summary: str
    structured_evidence_groups: list[dict[str, object]]
    prose_evidence_groups: list[dict[str, object]]


@dataclass(slots=True)
class GroundedAnswerResponse:
    """Normalized grounded-answer response."""

    answer: str
    evidence_summary: str
    sources: list[AnswerCitation]
    uncertainty: str | None
    insufficient_evidence: bool


@dataclass(slots=True)
class AnswerProviderConfig:
    """Provider-specific configuration for final answer generation."""

    provider: str
    model: str
    api_key: str


class AnswerProviderError(RuntimeError):
    """Base error for answer-provider failures."""


class AnswerProviderConfigError(AnswerProviderError):
    """Raised when the answer-provider configuration is missing or invalid."""


ANSWER_SCHEMA_NAME = "grounded_answer"
ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string"},
        "evidence_summary": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page_number": {"type": "integer"},
                    "section_title": {"type": ["string", "null"]},
                    "table_title": {"type": ["string", "null"]},
                    "row_index": {"type": ["integer", "null"]},
                    "chunk_type": {"type": "string"},
                    "source_note": {"type": ["string", "null"]},
                },
                "required": [
                    "page_number",
                    "section_title",
                    "table_title",
                    "row_index",
                    "chunk_type",
                    "source_note",
                ],
            },
        },
        "uncertainty": {"type": ["string", "null"]},
        "insufficient_evidence": {"type": "boolean"},
    },
    "required": [
        "answer",
        "evidence_summary",
        "sources",
        "uncertainty",
        "insufficient_evidence",
    ],
}
ANSWER_SYSTEM_PROMPT = """You are a technical datasheet QA assistant.
Your job is to answer the user's question using only the provided retrieval evidence from an electrical or electronic component datasheet.

The datasheet may describe MCUs, sensors, connectors, passives, antennas, regulators, logic ICs, memory devices, RF parts, MOSFETs, LEDs, LED drivers, power controllers, interface ICs, clocks, or other electronic components.
Do not assume the component family unless the supplied evidence supports it.

Return only JSON matching the provided schema.

Core behavior:
- Answer the user's actual engineering question, not just summarize the retrieved text.
- Use only the evidence in the request payload.
- Do not use outside knowledge.
- Do not invent missing values, pin names, package names, ratings, tolerances, dimensions, protocols, limits, thermal data, timing numbers, or electrical characteristics.
- Set insufficient_evidence=true only when the provided evidence cannot answer the user's main intent.
- If the main question is answerable but some secondary detail is missing, keep insufficient_evidence=false and put the caveat in uncertainty.
- Every material claim must be supported by one or more sources from the supplied evidence.
- Prefer precise, compact, verifiable wording over speculative synthesis.
- Preserve distinctions between variants, packages, configurations, modes, voltage ranges, temperatures, grades, and operating conditions when the evidence does.
- Do not collapse package-specific mappings into one answer when the evidence shows they differ.
- Distinguish canonical signal/function names from package-specific pin, ball, pad, lead, or terminal identifiers.

Evidence handling:
- Prefer explicit structured evidence such as terminal mapping tables, pin assignment tables, electrical characteristics tables, timing tables, control/register tables, ordering tables, package tables, application notes inside the datasheet, and explicit prose statements.
- Treat directly relevant tables as stronger evidence than nearby unrelated prose when the question asks for structured values or mappings.
- Ignore irrelevant retrieved rows even if they appear high-scoring.
- Do not let package summary counts, BOM-like rows, mechanical drawings, or noisy schematic text override clearer direct evidence for functional questions.
- Use application circuits and diagrams only when they clearly support the requested fact; do not treat them as normative over explicit tables unless the evidence says so.
- If multiple retrieved rows repeat the same fact, deduplicate the answer while preserving meaningful package or variant differences.

Task-specific guidance:
- For broad conceptual questions about a named module, peripheral, block, interface, or feature, interpret the question as asking how that device-specific block operates according to this datasheet, not as asking for a generic textbook tutorial.
- For such conceptual questions, give a useful device-specific operational summary when the evidence supports one, even if the datasheet evidence does not include every protocol subdetail or a full state-machine tutorial.
- For terminal or function mapping questions, look for explicit matches in fields such as Name, Signal, Terminal, Function, Dedicated function, Description, Notes, or equivalent table headings.
- Do not confuse generic capability with an explicitly assigned function. For example, GPIO-capable, clock-capable, analog-capable, or alternate-function-capable does not prove a specific requested function unless the evidence maps it directly.
- If the evidence shows a dedicated signal rather than a GPIO-style name, report the dedicated signal name instead of inventing a GPIO mapping.
- If the same function appears on different package pins or balls by package, say that the package-specific pin varies and preserve the mapping as shown.
- For electrical or timing questions, preserve units and min/typ/max distinctions, and include conditions, supply, load, mode, frequency, or temperature qualifiers when present.
- Do not merge absolute maximum ratings with recommended operating conditions.
- Do not treat typical values as guaranteed limits.
- For passive, LED, MOSFET, regulator, or power questions, preserve value, polarity, rating, threshold, dissipation, thermal, current, voltage, package, and condition details only when explicitly present.
- For connector or pinout questions, preserve numbering, orientation, direction, no-connect, shield, ground, power, detect, reserved, and keying details when relevant.
- For RF or antenna questions, preserve band, impedance, matching, feed, keepout, grounding, orientation, and tuning notes only if shown.

Output behavior:
- Keep the answer focused and useful.
- Keep the total response compact enough to fit comfortably within the response budget.
- Keep the `answer` field concise, preferably under 180 words unless a shorter answer would omit essential grounded detail.
- Keep `evidence_summary` brief, preferably under 60 words.
- Cite only the most relevant sources, usually no more than 4 unless additional citations are necessary to preserve package or variant differences.
- If the answer is naturally a mapping or comparison, present it cleanly in prose that could be rendered from the JSON answer field without inventing new structure outside the schema.
- Use evidence_summary to briefly summarize what evidence families or regions supported the answer.
- Use uncertainty for caveats, missing package-specific detail, ambiguous evidence, or partial support.
- In sources, cite the most relevant supporting items using the exact provenance available in the payload: page_number, section_title, table_title, row_index, and chunk_type.

Failure behavior:
- If the evidence does not actually support the requested conclusion, do not guess.
- If only part of the question is supported, answer that part and explicitly mark the rest as unknown from the provided evidence.
- If the evidence is too weak, sparse, or indirect, say so plainly and set insufficient_evidence=true."""

ANSWER_SEMANTICS_EXAMPLES = """
Example 1:
- Question: "How does the UART peripheral work?"
- Evidence supports ENABLE, BAUDRATE, CONFIG, PSEL.*, DMA buffers, timeout, and error behavior, but not a full protocol tutorial.
- Correct behavior: answer the device-specific UARTE operation summary, set insufficient_evidence=false, and mention missing protocol/state-machine depth in uncertainty.

Example 2:
- Question: "Which exact interrupt fires after every transmitted byte?"
- Evidence only shows generic register summaries and pin mappings with no direct interrupt/event confirmation.
- Correct behavior: do not guess, set insufficient_evidence=true, and explain that the retrieved evidence does not answer the main question.
"""


def load_answer_provider_config() -> AnswerProviderConfig:
    """Load the grounded-answer provider configuration from the environment."""

    provider = os.getenv("DATASHEET_RAG_ANSWER_PROVIDER", "").strip().lower()
    model = os.getenv("DATASHEET_RAG_ANSWER_MODEL", "").strip()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    missing: list[str] = []
    if not provider:
        missing.append("DATASHEET_RAG_ANSWER_PROVIDER")
    if not model:
        missing.append("DATASHEET_RAG_ANSWER_MODEL")
    if missing:
        raise AnswerProviderConfigError(
            f"Answer provider not configured ({', '.join(missing)} missing)."
        )
    if provider != "openai":
        raise AnswerProviderConfigError(
            f"Unsupported answer provider '{provider}'. Supported providers: openai."
        )
    if not api_key:
        raise AnswerProviderConfigError("Answer provider not configured (OPENAI_API_KEY missing).")
    return AnswerProviderConfig(provider=provider, model=model, api_key=api_key)


def generate_with_provider(
    *,
    request: GroundedAnswerRequest,
    config: AnswerProviderConfig,
) -> GroundedAnswerResponse:
    """Generate a grounded answer using the selected provider."""

    if config.provider == "openai":
        return _generate_grounded_answer_via_openai(
            request=request,
            api_key=config.api_key,
            model=config.model,
        )
    raise AnswerProviderConfigError(
        f"Unsupported answer provider '{config.provider}'. Supported providers: openai."
    )


def _generate_grounded_answer_via_openai(
    *,
    request: GroundedAnswerRequest,
    api_key: str,
    model: str,
) -> GroundedAnswerResponse:
    """Use the OpenAI Responses API to produce a grounded answer."""

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": f"{ANSWER_SYSTEM_PROMPT}\n\n{ANSWER_SEMANTICS_EXAMPLES.strip()}"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": request.question,
                        "intent": request.intent,
                        "primary_subject": request.primary_subject,
                        "retrieval_summary": request.retrieval_summary,
                        "coverage_notes": request.coverage_notes,
                        "evidence_summary": request.evidence_summary,
                        "structured_evidence_groups": request.structured_evidence_groups,
                        "prose_evidence_groups": request.prose_evidence_groups,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": ANSWER_SCHEMA_NAME,
                "strict": True,
                "schema": ANSWER_SCHEMA,
            }
        },
        max_output_tokens=2000,
    )
    raw_text = _extract_response_text(response)
    payload = _parse_provider_json_payload(raw_text)
    return _validate_grounded_answer_payload(payload)


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
                raise AnswerProviderError(f"Answer-provider refusal: {refusal}")
    raise AnswerProviderError("Answer provider returned no usable text output.")


def _parse_provider_json_payload(raw_text: str) -> dict[str, object]:
    """Parse provider JSON robustly and fail with a clean provider error."""

    candidates = _candidate_json_texts(raw_text)
    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"{exc.msg} at line {exc.lineno} column {exc.colno}")
            continue
        if not isinstance(payload, dict):
            raise AnswerProviderError("Answer provider payload must be a JSON object.")
        return payload

    preview = _normalize_whitespace(raw_text)[:240]
    error_summary = errors[0] if errors else "unparseable JSON payload"
    if _looks_like_truncated_json(raw_text, error_summary):
        error_summary = f"{error_summary}; output may have been truncated"
    raise AnswerProviderError(
        "Answer provider returned malformed JSON output "
        f"({error_summary}). Raw output preview: {preview}"
    )


def _candidate_json_texts(raw_text: str) -> list[str]:
    """Produce likely JSON candidates from provider text output."""

    text = raw_text.strip()
    if not text:
        return []

    candidates: list[str] = [text]
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1].strip())

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_candidates.append(normalized)
    return unique_candidates


def _normalize_whitespace(text: str) -> str:
    """Collapse whitespace for concise error previews."""

    return re.sub(r"\s+", " ", text).strip()


def _looks_like_truncated_json(raw_text: str, error_summary: str) -> bool:
    """Heuristically detect output that likely ended before JSON completed."""

    text = raw_text.strip()
    if not text:
        return False
    if "Unterminated string" in error_summary:
        return True
    if text.count("{") > text.count("}") or text.count("[") > text.count("]"):
        return True
    return False


def _validate_grounded_answer_payload(payload: dict[str, object]) -> GroundedAnswerResponse:
    """Validate and normalize a grounded-answer payload."""

    if not isinstance(payload, dict):
        raise AnswerProviderError("Answer provider payload must be a JSON object.")

    answer = payload.get("answer")
    evidence_summary = payload.get("evidence_summary")
    sources_payload = payload.get("sources")
    uncertainty = payload.get("uncertainty")
    insufficient_evidence = payload.get("insufficient_evidence")

    if not isinstance(answer, str) or not answer.strip():
        raise AnswerProviderError("Answer provider field 'answer' must be a non-empty string.")
    if not isinstance(evidence_summary, str) or not evidence_summary.strip():
        raise AnswerProviderError("Answer provider field 'evidence_summary' must be a non-empty string.")
    if not isinstance(sources_payload, list):
        raise AnswerProviderError("Answer provider field 'sources' must be an array.")
    if uncertainty is not None and not isinstance(uncertainty, str):
        raise AnswerProviderError("Answer provider field 'uncertainty' must be a string or null.")
    if not isinstance(insufficient_evidence, bool):
        raise AnswerProviderError("Answer provider field 'insufficient_evidence' must be a boolean.")

    sources: list[AnswerCitation] = []
    for item in sources_payload:
        if not isinstance(item, dict):
            raise AnswerProviderError("Answer provider sources must be objects.")
        page_number = item.get("page_number")
        chunk_type = item.get("chunk_type")
        if not isinstance(page_number, int):
            raise AnswerProviderError("Answer citation field 'page_number' must be an integer.")
        if not isinstance(chunk_type, str) or not chunk_type.strip():
            raise AnswerProviderError("Answer citation field 'chunk_type' must be a non-empty string.")
        row_index = item.get("row_index")
        if row_index is not None and not isinstance(row_index, int):
            raise AnswerProviderError("Answer citation field 'row_index' must be an integer or null.")
        section_title = item.get("section_title")
        table_title = item.get("table_title")
        source_note = item.get("source_note")
        if section_title is not None and not isinstance(section_title, str):
            raise AnswerProviderError("Answer citation field 'section_title' must be a string or null.")
        if table_title is not None and not isinstance(table_title, str):
            raise AnswerProviderError("Answer citation field 'table_title' must be a string or null.")
        if source_note is not None and not isinstance(source_note, str):
            raise AnswerProviderError("Answer citation field 'source_note' must be a string or null.")
        sources.append(
            AnswerCitation(
                page_number=page_number,
                section_title=section_title,
                table_title=table_title,
                row_index=row_index,
                chunk_type=chunk_type.strip(),
                source_note=source_note.strip() if isinstance(source_note, str) and source_note.strip() else None,
            )
        )

    if not insufficient_evidence and not sources:
        raise AnswerProviderError("Grounded answers must include at least one source citation.")

    return GroundedAnswerResponse(
        answer=answer.strip(),
        evidence_summary=evidence_summary.strip(),
        sources=sources,
        uncertainty=uncertainty.strip() if isinstance(uncertainty, str) and uncertainty.strip() else None,
        insufficient_evidence=insufficient_evidence,
    )
