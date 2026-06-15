"""Grounded answer orchestration over evidence packs."""

from __future__ import annotations

from dataclasses import dataclass
import re

from datasheet_rag.entities import QueryResponse
from datasheet_rag.llm_provider import (
    AnswerCitation,
    AnswerProviderConfig,
    GroundedAnswerRequest,
    generate_with_provider,
    load_answer_provider_config,
)


@dataclass(slots=True)
class EvidenceSufficiency:
    """Deterministic local assessment of whether evidence can support answering."""

    sufficient: bool
    reason: str
    concrete_source_count: int
    structured_source_count: int
    prose_source_count: int
    compatible_family_count: int
    max_quality_score: float
    meaningful_explanatory_source_count: int


@dataclass(slots=True)
class AnswerResult:
    """Final grounded answer or grounded non-answer."""

    question: str
    answer: str
    evidence_summary: str
    sources: list[AnswerCitation]
    uncertainty: str | None
    insufficient_evidence: bool
    provider_mode: str


def generate_grounded_answer(evidence_response: QueryResponse) -> AnswerResult:
    """Generate a grounded answer from an evidence response."""

    evidence_summary = build_evidence_summary(evidence_response)
    sufficiency = assess_evidence_sufficiency(evidence_response)
    fallback_sources = collect_supporting_citations(evidence_response)
    if not sufficiency.sufficient:
        return AnswerResult(
            question=evidence_response.question,
            answer=(
                "I could not answer from the retrieved evidence alone. "
                f"Evidence was insufficient: {sufficiency.reason}"
            ),
            evidence_summary=evidence_summary,
            sources=fallback_sources,
            uncertainty=sufficiency.reason,
            insufficient_evidence=True,
            provider_mode="local_insufficient",
        )

    config = load_answer_provider_config()
    request = build_grounded_answer_request(evidence_response)
    provider_response = generate_with_provider(request=request, config=config)
    provider_response = normalize_provider_answer(
        evidence_response=evidence_response,
        provider_response=provider_response,
    )
    return AnswerResult(
        question=evidence_response.question,
        answer=provider_response.answer,
        evidence_summary=provider_response.evidence_summary,
        sources=provider_response.sources,
        uncertainty=provider_response.uncertainty,
        insufficient_evidence=provider_response.insufficient_evidence,
        provider_mode=config.provider,
    )


def build_grounded_answer_request(
    evidence_response: QueryResponse,
) -> GroundedAnswerRequest:
    """Convert a query/evidence response into a provider-agnostic grounded-answer request."""

    return GroundedAnswerRequest(
        question=evidence_response.question,
        intent=evidence_response.intent,
        primary_subject=evidence_response.primary_subject,
        retrieval_summary=evidence_response.retrieval_summary,
        coverage_notes=list(evidence_response.coverage_notes),
        evidence_summary=build_evidence_summary(evidence_response),
        structured_evidence_groups=[
            {
                "page_number": group.get("page_number"),
                "table_index": group.get("table_index"),
                "section_title": group.get("section_title"),
                "table_title": group.get("table_title"),
                "evidence_family": group.get("evidence_family"),
                "quality_score": group.get("quality_score"),
                "summary": group.get("summary"),
                "group_score": group.get("group_score"),
                "items": [
                    {
                        "chunk_type": item.get("chunk_type"),
                        "chunk_index": item.get("chunk_index"),
                        "row_index": item.get("row_index"),
                        "row_type": item.get("row_type"),
                        "entity_family": item.get("entity_family"),
                        "entity_display_text": item.get("entity_display_text"),
                        "headers": item.get("headers"),
                        "cells": item.get("cells"),
                        "text": item.get("text"),
                    }
                    for item in group.get("items", [])
                ],
            }
            for group in evidence_response.structured_evidence_groups
        ],
        prose_evidence_groups=[
            {
                "page_number": group.get("page_number"),
                "table_index": group.get("table_index"),
                "section_title": group.get("section_title"),
                "table_title": group.get("table_title"),
                "evidence_family": group.get("evidence_family"),
                "quality_score": group.get("quality_score"),
                "summary": group.get("summary"),
                "group_score": group.get("group_score"),
                "items": [
                    {
                        "chunk_type": item.get("chunk_type"),
                        "chunk_index": item.get("chunk_index"),
                        "row_index": item.get("row_index"),
                        "row_type": item.get("row_type"),
                        "entity_family": item.get("entity_family"),
                        "entity_display_text": item.get("entity_display_text"),
                        "headers": item.get("headers"),
                        "cells": item.get("cells"),
                        "text": item.get("text"),
                    }
                    for item in group.get("items", [])
                ],
            }
            for group in evidence_response.prose_evidence_groups
        ],
    )


def assess_evidence_sufficiency(evidence_response: QueryResponse) -> EvidenceSufficiency:
    """Decide whether the evidence pack is strong enough to justify answer generation."""

    groups = evidence_response.evidence_groups
    structured_groups = evidence_response.structured_evidence_groups
    prose_groups = evidence_response.prose_evidence_groups
    if not groups:
        return EvidenceSufficiency(
            sufficient=False,
            reason="no evidence groups were retrieved",
            concrete_source_count=0,
            structured_source_count=0,
            prose_source_count=0,
            compatible_family_count=0,
            max_quality_score=0.0,
            meaningful_explanatory_source_count=0,
        )

    concrete_sources = 0
    structured_sources = 0
    prose_sources = 0
    compatible_families = 0
    max_quality_score = 0.0
    meaningful_explanatory_sources = 0
    compatible_values = _compatible_evidence_families(evidence_response.intent)
    for group in groups:
        quality_score = float(group.get("quality_score") or 0.0)
        max_quality_score = max(max_quality_score, quality_score)
        evidence_family = str(group.get("evidence_family") or "")
        if evidence_family in compatible_values:
            compatible_families += 1
        items = group.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            concrete_sources += 1
            if (
                item.get("chunk_type") == "table_row"
                or item.get("row_index") is not None
                or item.get("headers") is not None
                or item.get("cells") is not None
            ):
                structured_sources += 1
            else:
                prose_sources += 1
            if _is_meaningful_explanatory_item(item=item, evidence_family=evidence_family):
                meaningful_explanatory_sources += 1

    if concrete_sources == 0:
        reason = "retrieval returned no concrete source items"
    elif max_quality_score < 0.32:
        reason = f"top evidence quality was too low ({max_quality_score:.2f})"
    elif compatible_families == 0:
        reason = f"retrieved evidence families were not compatible with intent '{evidence_response.intent}'"
    elif evidence_response.intent == "generic" and meaningful_explanatory_sources == 0:
        reason = "conceptual questions require meaningful explanatory evidence about the named device feature"
    elif structured_sources == 0 and concrete_sources < 2:
        reason = "only sparse prose evidence was retrieved"
    else:
        return EvidenceSufficiency(
            sufficient=True,
            reason="evidence was sufficient",
            concrete_source_count=concrete_sources,
            structured_source_count=structured_sources,
            prose_source_count=prose_sources,
            compatible_family_count=compatible_families,
            max_quality_score=max_quality_score,
            meaningful_explanatory_source_count=meaningful_explanatory_sources,
        )

    return EvidenceSufficiency(
        sufficient=False,
        reason=reason,
        concrete_source_count=concrete_sources,
        structured_source_count=structured_sources,
        prose_source_count=prose_sources,
        compatible_family_count=compatible_families,
        max_quality_score=max_quality_score,
        meaningful_explanatory_source_count=meaningful_explanatory_sources,
    )


def build_evidence_summary(evidence_response: QueryResponse) -> str:
    """Build a compact local evidence summary from the selected evidence groups."""

    structured_groups = evidence_response.structured_evidence_groups
    prose_groups = evidence_response.prose_evidence_groups
    if not structured_groups and not prose_groups:
        return "No evidence groups were retrieved."
    parts: list[str] = []
    for label, groups in (("structured", structured_groups), ("prose", prose_groups)):
        if not groups:
            continue
        group_parts: list[str] = []
        for group in groups[:2]:
            page_number = group.get("page_number")
            evidence_family = group.get("evidence_family") or "unknown"
            section_title = group.get("section_title")
            table_title = group.get("table_title")
            items = group.get("items", [])
            label_parts = [f"page {page_number}", str(evidence_family)]
            if section_title:
                label_parts.append(f"section '{section_title}'")
            if table_title:
                label_parts.append(f"table '{table_title}'")
            label_parts.append(f"{len(items) if isinstance(items, list) else 0} item(s)")
            group_parts.append(", ".join(label_parts))
        parts.append(f"{label}: " + "; ".join(group_parts))
    return "; ".join(parts)


def collect_supporting_citations(evidence_response: QueryResponse) -> list[AnswerCitation]:
    """Collect compact deterministic citations from the evidence pack."""

    citations: list[AnswerCitation] = []
    seen: set[tuple[int, str, str | None, int | None]] = set()
    for group_collection in (evidence_response.structured_evidence_groups, evidence_response.prose_evidence_groups):
        for group in group_collection:
            page_number = int(group.get("page_number") or 0)
            section_title = group.get("section_title")
            table_title = group.get("table_title")
            for item in group.get("items", []) if isinstance(group.get("items"), list) else []:
                if not isinstance(item, dict):
                    continue
                chunk_type = str(item.get("chunk_type") or "unknown").strip() or "unknown"
                row_index = item.get("row_index")
                row_index = row_index if isinstance(row_index, int) else None
                key = (page_number, chunk_type, table_title if isinstance(table_title, str) else None, row_index)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(
                    AnswerCitation(
                        page_number=page_number,
                        section_title=section_title if isinstance(section_title, str) and section_title.strip() else None,
                        table_title=table_title if isinstance(table_title, str) and table_title.strip() else None,
                        row_index=row_index,
                        chunk_type=chunk_type,
                        source_note=_build_source_note(item),
                    )
                )
                if len(citations) >= 6:
                    return citations
    return citations


def _build_source_note(item: dict[str, object]) -> str | None:
    """Build a short citation note from one evidence item."""

    entity_display_text = item.get("entity_display_text")
    if isinstance(entity_display_text, str) and entity_display_text.strip():
        return entity_display_text.strip()
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    return text[:120]


def _compatible_evidence_families(intent: str) -> set[str]:
    """Map query intent to acceptable evidence families for answering."""

    mapping = {
        "terminal_lookup": {"terminal_mapping", "feature_summary", "generic_text"},
        "feature_to_terminal": {"terminal_mapping", "feature_summary", "generic_text"},
        "variant_package": {"package_variant", "ordering_info", "generic_text"},
        "register_control": {"control_definition", "feature_summary", "generic_text"},
        "spec_lookup": {"electrical_spec", "timing_spec", "feature_summary", "generic_text"},
        "generic": {
            "terminal_mapping",
            "package_variant",
            "ordering_info",
            "control_definition",
            "electrical_spec",
            "timing_spec",
            "feature_summary",
            "generic_text",
        },
    }
    return mapping.get(intent, mapping["generic"])


def normalize_provider_answer(
    *,
    evidence_response: QueryResponse,
    provider_response: GroundedAnswerResponse,
) -> GroundedAnswerResponse:
    """Convert over-conservative provider outputs into supported partial answers."""

    if not provider_response.insufficient_evidence:
        return provider_response
    if not _looks_like_supported_partial_answer(
        evidence_response=evidence_response,
        provider_response=provider_response,
    ):
        return provider_response
    return GroundedAnswerResponse(
        answer=provider_response.answer,
        evidence_summary=provider_response.evidence_summary,
        sources=provider_response.sources,
        uncertainty=provider_response.uncertainty,
        insufficient_evidence=False,
    )


def _looks_like_supported_partial_answer(
    *,
    evidence_response: QueryResponse,
    provider_response: GroundedAnswerResponse,
) -> bool:
    """Detect when the answer supports the main intent but caveats missing detail."""

    if not provider_response.sources:
        return False
    answer_text = provider_response.answer.strip()
    if not _is_substantive_grounded_answer(answer_text):
        return False
    if _looks_like_total_non_answer(answer_text):
        return False
    if not provider_response.evidence_summary.strip():
        return False
    if evidence_response.intent not in {"generic", "register_control", "spec_lookup", "feature_to_terminal", "terminal_lookup"}:
        return False
    return _has_partial_support_caveat(
        answer_text=answer_text,
        uncertainty=provider_response.uncertainty,
    )


def _is_meaningful_explanatory_item(*, item: dict[str, object], evidence_family: str) -> bool:
    """Identify source items that can support a device-specific operational summary."""

    text = str(item.get("text") or "").strip()
    if len(text) < 80:
        return False
    lowered = text.lower()
    if evidence_family in {"control_definition", "feature_summary", "application_circuit"}:
        return True
    explanatory_markers = (
        "enable",
        "configuration",
        "configure",
        "baud",
        "dma",
        "buffer",
        "timeout",
        "error",
        "interrupt",
        "event",
        "task",
        "operation",
        "peripheral",
        "pin select",
        "psel",
        "flow control",
    )
    return sum(marker in lowered for marker in explanatory_markers) >= 2


def _is_substantive_grounded_answer(answer_text: str) -> bool:
    """Decide whether an answer is substantive enough to address the main question."""

    word_count = len(re.findall(r"\w+", answer_text))
    return word_count >= 18


def _looks_like_total_non_answer(text: str) -> bool:
    """Detect explicit non-answer phrasing."""

    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "could not answer",
            "cannot answer",
            "can't answer",
            "insufficient evidence",
            "not enough evidence",
            "unknown from the provided evidence",
            "cannot determine",
            "unable to determine",
            "the evidence does not answer",
        )
    )


def _has_partial_support_caveat(*, answer_text: str, uncertainty: str | None) -> bool:
    """Detect caveat-style language that indicates partial but useful support."""

    caveat_candidates = [answer_text]
    if uncertainty:
        caveat_candidates.append(uncertainty)
    lowered = " ".join(part.lower() for part in caveat_candidates if part).strip()
    if not lowered:
        return False
    if any(
        phrase in lowered
        for phrase in (
            "does not provide a full",
            "does not include a full",
            "does not provide a complete",
            "does not include a complete",
            "not a full",
            "not a complete",
            "not exhaustive",
            "not fully",
            "does not cover all",
            "does not provide all",
            "missing",
            "full protocol tutorial",
            "state-machine",
            "interrupt list",
            "additional detail",
            "caveat",
            "however",
        )
    ):
        return True
    if uncertainty and not _looks_like_total_non_answer(uncertainty):
        return True
    return False
