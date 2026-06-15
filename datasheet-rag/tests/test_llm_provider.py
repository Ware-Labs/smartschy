from __future__ import annotations

import json
import types

import pytest

from datasheet_rag import llm_provider


def _sample_request() -> llm_provider.GroundedAnswerRequest:
    return llm_provider.GroundedAnswerRequest(
        question="What functions are available on P1.04?",
        intent="terminal_lookup",
        primary_subject="P1.04",
        retrieval_summary="summary",
        coverage_notes=["note"],
        evidence_summary="page 2 terminal_mapping 1 item",
        structured_evidence_groups=[
            {
                "page_number": 2,
                "table_index": 0,
                "section_title": "Pin assignments",
                "table_title": "Table 1: Pin assignments",
                "evidence_family": "terminal_mapping",
                "quality_score": 0.88,
                "summary": "terminal mapping",
                "group_score": 320.0,
                "items": [
                    {
                        "chunk_type": "table_row",
                        "chunk_index": 0,
                        "row_index": 2,
                        "row_type": "data_row",
                        "entity_family": "signal_or_terminal",
                        "entity_display_text": "P1.04",
                        "headers": ["Pin", "Name", "Function", "Description"],
                        "cells": ["12", "P1.04", "GPIO / UARTE TXD", "GPIO or TXD"],
                        "text": "Pin: 12. Name: P1.04. Function: GPIO / UARTE TXD.",
                    }
                ],
            }
        ],
        prose_evidence_groups=[],
    )


def test_load_answer_provider_config_requires_separate_answer_env(monkeypatch) -> None:
    monkeypatch.delenv("DATASHEET_RAG_ANSWER_PROVIDER", raising=False)
    monkeypatch.delenv("DATASHEET_RAG_ANSWER_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(llm_provider.AnswerProviderConfigError) as exc:
        llm_provider.load_answer_provider_config()

    assert "DATASHEET_RAG_ANSWER_PROVIDER" in str(exc.value)


def test_validate_grounded_answer_payload_rejects_malformed_output() -> None:
    with pytest.raises(llm_provider.AnswerProviderError) as exc:
        llm_provider._validate_grounded_answer_payload(
            {
                "answer": "",
                "evidence_summary": "summary",
                "sources": [],
                "uncertainty": None,
                "insufficient_evidence": False,
            }
        )

    assert "non-empty string" in str(exc.value)


def test_generate_grounded_answer_via_openai_sends_valid_schema_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "answer": "P1.04 provides GPIO / UARTE TXD.",
                        "evidence_summary": "Pin-assignment table evidence on page 2.",
                        "sources": [
                            {
                                "page_number": 2,
                                "section_title": "Pin assignments",
                                "table_title": "Table 1: Pin assignments",
                                "row_index": 2,
                                "chunk_type": "table_row",
                                "source_note": "P1.04",
                            }
                        ],
                        "uncertainty": None,
                        "insufficient_evidence": False,
                    }
                )
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            assert api_key == "test-key"
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    response = llm_provider._generate_grounded_answer_via_openai(
        request=_sample_request(),
        api_key="test-key",
        model="gpt-test",
    )

    assert response.answer == "P1.04 provides GPIO / UARTE TXD."
    assert captured["model"] == "gpt-test"
    schema = captured["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])


def test_generate_with_provider_raises_on_provider_refusal(monkeypatch) -> None:
    class _FakeResponses:
        def create(self, **kwargs):
            del kwargs
            return types.SimpleNamespace(
                output=[
                    types.SimpleNamespace(
                        type="message",
                        content=[types.SimpleNamespace(type="refusal", refusal="unsafe")],
                    )
                ]
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            del api_key
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    with pytest.raises(llm_provider.AnswerProviderError) as exc:
        llm_provider._generate_grounded_answer_via_openai(
            request=_sample_request(),
            api_key="test-key",
            model="gpt-test",
        )

    assert "refusal" in str(exc.value).lower()


def test_generate_grounded_answer_via_openai_parses_fenced_json_output(monkeypatch) -> None:
    class _FakeResponses:
        def create(self, **kwargs):
            del kwargs
            payload = {
                "answer": "P1.04 provides GPIO / UARTE TXD.",
                "evidence_summary": "Pin-assignment table evidence on page 2.",
                "sources": [
                    {
                        "page_number": 2,
                        "section_title": "Pin assignments",
                        "table_title": "Table 1: Pin assignments",
                        "row_index": 2,
                        "chunk_type": "table_row",
                        "source_note": "P1.04",
                    }
                ],
                "uncertainty": None,
                "insufficient_evidence": False,
            }
            return types.SimpleNamespace(output_text=f"```json\n{json.dumps(payload)}\n```")

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            del api_key
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    response = llm_provider._generate_grounded_answer_via_openai(
        request=_sample_request(),
        api_key="test-key",
        model="gpt-test",
    )

    assert response.answer == "P1.04 provides GPIO / UARTE TXD."


def test_generate_grounded_answer_via_openai_parses_json_with_surrounding_text(monkeypatch) -> None:
    class _FakeResponses:
        def create(self, **kwargs):
            del kwargs
            payload = {
                "answer": "P1.04 provides GPIO / UARTE TXD.",
                "evidence_summary": "Pin-assignment table evidence on page 2.",
                "sources": [
                    {
                        "page_number": 2,
                        "section_title": "Pin assignments",
                        "table_title": "Table 1: Pin assignments",
                        "row_index": 2,
                        "chunk_type": "table_row",
                        "source_note": "P1.04",
                    }
                ],
                "uncertainty": None,
                "insufficient_evidence": False,
            }
            return types.SimpleNamespace(
                output_text=f"Here is the grounded answer.\n{json.dumps(payload)}\nThanks."
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            del api_key
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    response = llm_provider._generate_grounded_answer_via_openai(
        request=_sample_request(),
        api_key="test-key",
        model="gpt-test",
    )

    assert response.answer == "P1.04 provides GPIO / UARTE TXD."


def test_generate_grounded_answer_via_openai_raises_clean_error_on_malformed_json(monkeypatch) -> None:
    class _FakeResponses:
        def create(self, **kwargs):
            del kwargs
            return types.SimpleNamespace(
                output_text='{"answer":"UART overview","evidence_summary":"summary","sources":[{"page_number":1}'
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            del api_key
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    with pytest.raises(llm_provider.AnswerProviderError) as exc:
        llm_provider._generate_grounded_answer_via_openai(
            request=_sample_request(),
            api_key="test-key",
            model="gpt-test",
        )

    message = str(exc.value)
    assert "malformed json" in message.lower()
    assert "truncated" in message.lower() or "unterminated" in message.lower()
