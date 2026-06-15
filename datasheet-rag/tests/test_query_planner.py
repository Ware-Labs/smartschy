from __future__ import annotations

import json
import types

from datasheet_rag import query_planner


def test_plan_query_uses_openai_result_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")

    def fake_openai_plan(*, question: str, api_key: str, model: str) -> dict[str, object]:
        assert question == "which pins are meant for the LF and HF crystals?"
        assert api_key == "test-key"
        assert model == "gpt-test"
        return {
            "intent": "feature_to_terminal",
            "primary_subject": "LF/HF crystal pins",
            "must_include_terms": ["lf crystal", "hf crystal"],
            "should_include_terms": ["clock pins", "pin assignments", "32.768 kHz"],
            "identifier_terms": ["XL1", "XL2", "XC1", "XC2", "LFXO", "HFXO"],
            "table_family_preferences": ["pin_table"],
            "preferred_evidence_families": ["terminal_mapping", "feature_summary"],
            "section_hints": ["clock pins", "pin assignments"],
            "negative_terms": ["package variants"],
            "evidence_goal": "find all pin rows for LF/HF crystal signals",
            "subquestions": ["find LF crystal pins", "find HF crystal pins"],
        }

    monkeypatch.setattr(query_planner, "_plan_query_via_openai", fake_openai_plan)
    result = query_planner.plan_query("which pins are meant for the LF and HF crystals?")

    assert result.mode == "openai"
    assert result.spec.intent == "feature_to_terminal"
    assert result.spec.primary_subject == "LF/HF crystal pins"
    assert "XL1" in result.spec.identifier_terms
    assert "clock pins" in result.spec.section_hints
    assert "terminal_mapping" in result.spec.preferred_evidence_families
    assert "find lf crystal pins" in result.spec.subquestions
    assert result.spec.intent == "feature_to_terminal"


def test_plan_query_falls_back_when_openai_result_is_invalid(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")

    def fake_invalid_plan(*, question: str, api_key: str, model: str) -> dict[str, object]:
        del question, api_key, model
        return {
            "intent": "",
            "primary_subject": "broken payload",
        }

    monkeypatch.setattr(query_planner, "_plan_query_via_openai", fake_invalid_plan)
    result = query_planner.plan_query("which pins are meant for the LF and HF crystals?")

    assert result.mode == "local_fallback"
    assert result.spec.intent == "feature_to_terminal"
    assert "XL1" in result.spec.identifier_terms
    assert "clock pins" in result.spec.section_hints


def test_plan_query_openai_result_is_enriched_by_local_heuristics(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")

    def fake_openai_plan(*, question: str, api_key: str, model: str) -> dict[str, object]:
        del api_key, model
        assert question == "which pins are meant for the LF and HF crystals?"
        return {
            "intent": "terminal_lookup",
            "primary_subject": "LF and HF crystal pins",
            "must_include_terms": ["lf crystal", "hf crystal", "pins"],
            "should_include_terms": ["oscillator", "clock pins", "xtal"],
            "identifier_terms": [],
            "table_family_preferences": ["pin_table", "generic_table"],
            "preferred_evidence_families": ["terminal_mapping"],
            "section_hints": ["pin assignments", "clock source"],
            "negative_terms": ["ordering information"],
            "evidence_goal": "find crystal pin rows",
            "subquestions": [],
        }

    monkeypatch.setattr(query_planner, "_plan_query_via_openai", fake_openai_plan)
    result = query_planner.plan_query("which pins are meant for the LF and HF crystals?")

    assert result.mode == "openai"
    assert result.spec.intent == "feature_to_terminal"
    assert "XL1" in result.spec.identifier_terms
    assert "XC2" in result.spec.identifier_terms
    assert "hfxo" in result.spec.must_include_terms
    assert "find lf crystal pins" in result.spec.subquestions


def test_local_retrieval_spec_expands_crystal_aliases() -> None:
    spec = query_planner.build_local_retrieval_spec(
        "which pins are meant for the LF and HF crystals?"
    )

    assert spec.intent == "feature_to_terminal"
    assert "lf crystal" in spec.must_include_terms
    assert "hf crystal" in spec.must_include_terms
    assert "XL1" in spec.identifier_terms
    assert "XC2" in spec.identifier_terms
    assert "clock pins" in spec.section_hints
    assert "package variants" in spec.negative_terms
    assert "terminal_mapping" in spec.preferred_evidence_families
    assert "find LF crystal pins" in spec.subquestions


def test_validate_retrieval_spec_accepts_table_family_aliases_and_defaults() -> None:
    spec = query_planner._validate_retrieval_spec(
        {
            "intent": "feature_to_terminal",
            "primary_subject": " ",
            "must_include_terms": "LFXO",
            "should_include_terms": ["clock pins"],
            "identifier_terms": "XL1",
            "table_family_preferences": ["pin assignment table", "clock pins"],
            "preferred_evidence_families": ["terminal mapping", "feature table"],
            "section_hints": "clock pins",
            "negative_terms": [],
            "evidence_goal": "",
            "subquestions": "find LF crystal pins",
        },
        question="which pins are meant for the LF crystal?",
    )

    assert spec.intent == "feature_to_terminal"
    assert spec.primary_subject == "feature-to-terminal lookup"
    assert spec.must_include_terms == ["lfxo"]
    assert spec.identifier_terms == ["XL1"]
    assert spec.table_family_preferences == ["pin_table"]
    assert spec.preferred_evidence_families == ["terminal_mapping", "feature_summary"]
    assert spec.section_hints == ["clock pins"]
    assert spec.evidence_goal == "find pin rows for the requested feature signals"
    assert spec.subquestions == ["find lf crystal pins"]


def test_validate_retrieval_spec_accepts_intent_aliases() -> None:
    spec = query_planner._validate_retrieval_spec(
        {
            "intent": "package lookup",
            "primary_subject": "package",
            "must_include_terms": [],
            "should_include_terms": ["package"],
            "identifier_terms": ["XR1234B"],
            "table_family_preferences": ["ordering information"],
            "preferred_evidence_families": ["ordering table"],
            "section_hints": ["ordering information"],
            "negative_terms": [],
            "evidence_goal": "find package rows",
        },
        question="XR1234B package",
    )

    assert spec.intent == "variant_package"
    assert spec.table_family_preferences == ["variant_table"]
    assert spec.preferred_evidence_families == ["ordering_info"]


def test_planner_schema_required_matches_properties() -> None:
    assert set(query_planner.PLANNER_SCHEMA["required"]) == set(query_planner.PLANNER_SCHEMA["properties"])


def test_plan_query_via_openai_sends_valid_schema_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "intent": "feature_to_terminal",
                        "primary_subject": "LF/HF crystal pins",
                        "must_include_terms": ["lf crystal", "hf crystal"],
                        "should_include_terms": ["clock pins"],
                        "identifier_terms": ["XL1", "XC1"],
                        "table_family_preferences": ["pin_table"],
                        "preferred_evidence_families": ["terminal_mapping"],
                        "section_hints": ["clock pins"],
                        "negative_terms": ["package variants"],
                        "evidence_goal": "find crystal pin rows",
                        "subquestions": ["find LF crystal pins"],
                    }
                )
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            assert api_key == "test-key"
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    payload = query_planner._plan_query_via_openai(
        question="which pins are meant for the LF and HF crystals?",
        api_key="test-key",
        model="gpt-test",
    )

    assert payload["preferred_evidence_families"] == ["terminal_mapping"]
    assert captured["model"] == "gpt-test"
    schema = captured["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
