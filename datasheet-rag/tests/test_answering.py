from __future__ import annotations

from datasheet_rag import answering
from datasheet_rag.entities import QueryResponse
from datasheet_rag.llm_provider import (
    AnswerCitation,
    GroundedAnswerResponse,
)


def _sample_query_response(*, with_items: bool = True, quality_score: float = 0.88) -> QueryResponse:
    items = (
        [
            {
                "chunk_type": "table_row",
                "chunk_index": 0,
                "row_index": 2,
                "row_type": "data_row",
                "entity_family": "signal_or_terminal",
                "entity_display_text": "P1.04",
                "headers": ["Pin", "Name", "Function", "Description"],
                "cells": ["12", "P1.04", "GPIO / UARTE TXD", "GPIO or TXD"],
                "text": "Pin: 12. Name: P1.04. Function: GPIO / UARTE TXD. Description: GPIO or TXD.",
            }
        ]
        if with_items
        else []
    )
    return QueryResponse(
        question="What functions are available on P1.04?",
        intent="terminal_lookup",
        planner_mode="local_fallback",
        rerank_mode="local_fallback",
        primary_subject="P1.04",
        must_include_terms=["p1.04"],
        should_include_terms=["pin assignments"],
        identifier_terms=["P1.04"],
        table_family_preferences=["pin_table"],
        preferred_evidence_families=["terminal_mapping"],
        subquestions=[],
        section_hints=["pin assignments"],
        negative_terms=[],
        retrieval_summary="summary",
        candidate_family_summary={"terminal_mapping": 1},
        structured_evidence_groups=[
            {
                "page_number": 2,
                "table_index": 0,
                "section_title": "Pin assignments",
                "table_title": "Table 1: Pin assignments",
                "evidence_family": "terminal_mapping",
                "quality_score": quality_score,
                "summary": "page 2, terminal_mapping, 1 item",
                "group_score": 320.0,
                "items": items,
            }
        ],
        prose_evidence_groups=[],
        coverage_notes=["local fallback used"],
    )


def _conceptual_query_response(*, useful: bool = True) -> QueryResponse:
    prose_items = (
        [
            {
                "chunk_type": "text_chunk",
                "chunk_index": 0,
                "score": 225.0,
                "text": (
                    "ENABLE turns on the UARTE peripheral. BAUDRATE configures the baud rate. "
                    "CONFIG controls parity, hardware flow control, frame size, and packet timeout. "
                    "PSEL.TXD, PSEL.RXD, PSEL.CTS, and PSEL.RTS select the UART pins. "
                    "DMA.TX.PTR/MAXCNT and DMA.RX.PTR/MAXCNT configure EasyDMA transfers."
                ),
            }
        ]
        if useful
        else []
    )
    structured_items = [
        {
            "chunk_type": "table_row",
            "chunk_index": 0,
            "row_index": 0,
            "row_type": "data_row",
            "entity_family": "interface_or_feature",
            "entity_display_text": "UARTE",
            "headers": ["UARTE signal", "UARTE pin", "Direction", "Output value"],
            "cells": ["RXD", "As specified in PSEL.RXD", "Input", "Not applicable"],
            "text": (
                "Page 720. Table: GPIO configuration before enabling peripheral. "
                "UARTE signal: RXD. UARTE pin: As specified in PSEL.RXD. Direction: Input."
            ),
        }
    ]
    return QueryResponse(
        question="How does the UART peripheral work?",
        intent="generic",
        planner_mode="openai",
        rerank_mode="openai",
        primary_subject="UART peripheral",
        must_include_terms=["uart"],
        should_include_terms=["uarte", "baud rate", "configuration", "registers"],
        identifier_terms=[],
        table_family_preferences=["generic_table", "register_table"],
        preferred_evidence_families=["generic_text", "control_definition"],
        subquestions=[],
        section_hints=["uart overview", "operation"],
        negative_terms=[],
        retrieval_summary="summary",
        candidate_family_summary={"generic_text": 1, "control_definition": 1},
        structured_evidence_groups=[
            {
                "page_number": 720,
                "table_index": 0,
                "section_title": "Pin configuration",
                "table_title": "GPIO configuration before enabling peripheral",
                "evidence_family": "generic_text" if useful else "terminal_mapping",
                "quality_score": 0.71 if useful else 0.40,
                "summary": "page 720, generic_text, 1 item",
                "group_score": 320.0,
                "items": structured_items,
            }
        ],
        prose_evidence_groups=[
            {
                "page_number": 723,
                "section_title": "UARTE registers",
                "table_title": None,
                "evidence_family": "control_definition",
                "quality_score": 0.58 if useful else 0.20,
                "summary": "page 723, control_definition, 1 item",
                "group_score": 280.0,
                "items": prose_items,
            }
        ]
        if useful
        else [],
        coverage_notes=["openai planner used"],
    )


def test_build_grounded_answer_request_uses_structured_evidence_groups() -> None:
    response = _sample_query_response()

    request = answering.build_grounded_answer_request(response)

    assert request.question == "What functions are available on P1.04?"
    assert request.intent == "terminal_lookup"
    assert request.structured_evidence_groups[0]["page_number"] == 2
    assert request.structured_evidence_groups[0]["items"][0]["row_index"] == 2
    assert request.structured_evidence_groups[0]["items"][0]["headers"] == ["Pin", "Name", "Function", "Description"]


def test_assess_evidence_sufficiency_accepts_structured_terminal_evidence() -> None:
    response = _sample_query_response()

    sufficiency = answering.assess_evidence_sufficiency(response)

    assert sufficiency.sufficient is True
    assert sufficiency.structured_source_count == 1
    assert sufficiency.compatible_family_count == 1
    assert sufficiency.meaningful_explanatory_source_count == 0


def test_assess_evidence_sufficiency_accepts_conceptual_device_operation_evidence() -> None:
    response = _conceptual_query_response(useful=True)

    sufficiency = answering.assess_evidence_sufficiency(response)

    assert sufficiency.sufficient is True
    assert sufficiency.prose_source_count == 1
    assert sufficiency.meaningful_explanatory_source_count >= 1


def test_assess_evidence_sufficiency_rejects_empty_evidence() -> None:
    response = _sample_query_response(with_items=False)

    sufficiency = answering.assess_evidence_sufficiency(response)

    assert sufficiency.sufficient is False
    assert "no concrete source items" in sufficiency.reason


def test_assess_evidence_sufficiency_rejects_sparse_conceptual_noise() -> None:
    response = _conceptual_query_response(useful=False)

    sufficiency = answering.assess_evidence_sufficiency(response)

    assert sufficiency.sufficient is False
    assert "meaningful explanatory evidence" in sufficiency.reason


def test_collect_supporting_citations_normalizes_table_row_metadata() -> None:
    response = _sample_query_response()

    citations = answering.collect_supporting_citations(response)

    assert len(citations) == 1
    assert citations[0].page_number == 2
    assert citations[0].table_title == "Table 1: Pin assignments"
    assert citations[0].row_index == 2
    assert citations[0].chunk_type == "table_row"
    assert citations[0].source_note == "P1.04"


def test_generate_grounded_answer_returns_local_insufficiency_without_provider(monkeypatch) -> None:
    response = _sample_query_response(with_items=False)

    def fake_load_config():
        raise AssertionError("provider config should not be loaded for insufficient evidence")

    monkeypatch.setattr(answering, "load_answer_provider_config", fake_load_config)
    result = answering.generate_grounded_answer(response)

    assert result.insufficient_evidence is True
    assert result.provider_mode == "local_insufficient"
    assert "Evidence was insufficient" in result.answer


def test_generate_grounded_answer_uses_provider_for_strong_evidence(monkeypatch) -> None:
    response = _sample_query_response()

    monkeypatch.setattr(
        answering,
        "load_answer_provider_config",
        lambda: answering.AnswerProviderConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    def fake_generate_with_provider(*, request, config):
        assert request.question == "What functions are available on P1.04?"
        assert config.provider == "openai"
        return GroundedAnswerResponse(
            answer="P1.04 provides GPIO / UARTE TXD.",
            evidence_summary="Pin-assignment table evidence on page 2.",
            sources=[
                AnswerCitation(
                    page_number=2,
                    section_title="Pin assignments",
                    table_title="Table 1: Pin assignments",
                    row_index=2,
                    chunk_type="table_row",
                    source_note="P1.04",
                )
            ],
            uncertainty=None,
            insufficient_evidence=False,
        )

    monkeypatch.setattr(answering, "generate_with_provider", fake_generate_with_provider)
    result = answering.generate_grounded_answer(response)

    assert result.insufficient_evidence is False
    assert result.provider_mode == "openai"
    assert result.answer == "P1.04 provides GPIO / UARTE TXD."


def test_generate_grounded_answer_normalizes_partial_supported_provider_output(monkeypatch) -> None:
    response = _conceptual_query_response(useful=True)

    monkeypatch.setattr(
        answering,
        "load_answer_provider_config",
        lambda: answering.AnswerProviderConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    def fake_generate_with_provider(*, request, config):
        assert request.question == "How does the UART peripheral work?"
        assert config.provider == "openai"
        return GroundedAnswerResponse(
            answer=(
                "The evidence supports a device-specific UARTE operation summary: ENABLE turns the peripheral on, "
                "BAUDRATE and CONFIG configure serial behavior, PSEL.* selects TXD/RXD/CTS/RTS pins, and EasyDMA "
                "moves TX/RX data through DMA buffer registers."
            ),
            evidence_summary="Register summary prose plus GPIO/PSEL evidence for UARTE operation.",
            sources=[
                AnswerCitation(
                    page_number=723,
                    section_title="UARTE registers",
                    table_title=None,
                    row_index=None,
                    chunk_type="text_chunk",
                    source_note="ENABLE, BAUDRATE, CONFIG, DMA pointers",
                )
            ],
            uncertainty="The evidence does not provide a full UART protocol tutorial or exhaustive interrupt/state-machine detail.",
            insufficient_evidence=True,
        )

    monkeypatch.setattr(answering, "generate_with_provider", fake_generate_with_provider)
    result = answering.generate_grounded_answer(response)

    assert result.insufficient_evidence is False
    assert result.provider_mode == "openai"
    assert "UARTE operation summary" in result.answer
    assert result.uncertainty is not None


def test_generate_grounded_answer_keeps_true_insufficiency_for_non_answering_provider_output(monkeypatch) -> None:
    response = _conceptual_query_response(useful=True)

    monkeypatch.setattr(
        answering,
        "load_answer_provider_config",
        lambda: answering.AnswerProviderConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    def fake_generate_with_provider(*, request, config):
        assert request.question == "How does the UART peripheral work?"
        assert config.provider == "openai"
        return GroundedAnswerResponse(
            answer="I could not answer from the retrieved evidence alone because the provided evidence is insufficient.",
            evidence_summary="Only partial UART-related rows were retrieved.",
            sources=[
                AnswerCitation(
                    page_number=720,
                    section_title="Pin configuration",
                    table_title="GPIO configuration before enabling peripheral",
                    row_index=0,
                    chunk_type="table_row",
                    source_note="RXD pin mapping",
                )
            ],
            uncertainty="The retrieved evidence does not answer the main question.",
            insufficient_evidence=True,
        )

    monkeypatch.setattr(answering, "generate_with_provider", fake_generate_with_provider)
    result = answering.generate_grounded_answer(response)

    assert result.insufficient_evidence is True
    assert result.provider_mode == "openai"
    assert "could not answer" in result.answer.lower()
