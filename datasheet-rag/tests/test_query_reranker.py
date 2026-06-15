from __future__ import annotations

import json
import types

from datasheet_rag import query_planner, query_reranker
from datasheet_rag import storage


def test_rerank_query_groups_uses_openai_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")
    planner_result = query_planner.QueryPlannerResult(
        mode="openai",
        spec=query_planner.QueryRetrievalSpec(
            intent="feature_to_terminal",
            primary_subject="QSPI pins",
            must_include_terms=["qspi"],
            should_include_terms=["pin assignments"],
            identifier_terms=[],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            section_hints=["pin assignments"],
            negative_terms=[],
            evidence_goal="find QSPI pin rows",
            subquestions=[],
        ),
        note="OpenAI query planner used.",
    )

    def fake_rerank(**kwargs) -> dict[str, object]:
        assert kwargs["question"] == "What are the canonical pins for QSPI?"
        return {
            "ranked_group_ids": ["doc:2:0", "doc:3:0"],
            "reason_codes": [
                {"group_id": "doc:2:0", "reason_code": "terminal_mapping_exact"},
                {"group_id": "doc:3:0", "reason_code": "supporting_table"},
            ],
        }

    monkeypatch.setattr(query_reranker, "_rerank_query_groups_via_openai", fake_rerank)
    result = query_reranker.rerank_query_groups(
        question="What are the canonical pins for QSPI?",
        planner_result=planner_result,
        candidate_groups=[
            {"group_id": "doc:2:0", "evidence_family": "terminal_mapping", "quality_score": 0.9, "local_score": 300.0},
            {"group_id": "doc:3:0", "evidence_family": "feature_summary", "quality_score": 0.6, "local_score": 200.0},
        ],
    )

    assert result.mode == "openai"
    assert result.ranked_group_ids == ["doc:2:0", "doc:3:0"]
    assert result.reason_codes["doc:2:0"] == "terminal_mapping_exact"


def test_rerank_query_groups_falls_back_when_openai_fails(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")
    planner_result = query_planner.QueryPlannerResult(
        mode="openai",
        spec=query_planner.build_local_retrieval_spec("XR1234B package"),
        note="OpenAI query planner used.",
    )

    def fake_rerank(**kwargs) -> dict[str, object]:
        del kwargs
        raise ValueError("broken rerank payload")

    monkeypatch.setattr(query_reranker, "_rerank_query_groups_via_openai", fake_rerank)
    result = query_reranker.rerank_query_groups(
        question="XR1234B package",
        planner_result=planner_result,
        candidate_groups=[
            {"group_id": "doc:1:0", "evidence_family": "package_variant", "quality_score": 0.9, "local_score": 280.0},
            {"group_id": "doc:2:0", "evidence_family": "generic_text", "quality_score": 0.5, "local_score": 120.0},
        ],
    )

    assert result.mode == "local_fallback"
    assert result.ranked_group_ids == ["doc:1:0", "doc:2:0"]
    assert "broken rerank payload" in result.note


def test_rerank_schema_required_matches_properties() -> None:
    assert set(query_reranker.RERANK_SCHEMA["required"]) == set(query_reranker.RERANK_SCHEMA["properties"])


def test_rerank_query_groups_via_openai_sends_valid_schema_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}
    planner_result = query_planner.QueryPlannerResult(
        mode="openai",
        spec=query_planner.QueryRetrievalSpec(
            intent="feature_to_terminal",
            primary_subject="LF/HF crystal pins",
            must_include_terms=["lf crystal", "hf crystal"],
            should_include_terms=["clock pins"],
            identifier_terms=["XL1", "XC1"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            section_hints=["clock pins"],
            negative_terms=["package variants"],
            evidence_goal="find crystal pin rows",
            subquestions=["find LF crystal pins", "find HF crystal pins"],
        ),
        note="OpenAI query planner used.",
    )

    class _FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "ranked_group_ids": ["doc:863:0", "doc:866:0"],
                        "reason_codes": [
                            {"group_id": "doc:863:0", "reason_code": "terminal_mapping_exact"},
                            {"group_id": "doc:866:0", "reason_code": "terminal_mapping_support"},
                        ],
                    }
                )
            )

    class _FakeOpenAI:
        def __init__(self, api_key: str):
            assert api_key == "test-key"
            self.responses = _FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", types.SimpleNamespace(OpenAI=_FakeOpenAI))
    payload = query_reranker._rerank_query_groups_via_openai(
        question="which pins are meant for the LF and HF crystals?",
        planner_result=planner_result,
        candidate_groups=[
            {
                "group_id": "doc:863:0",
                "evidence_family": "terminal_mapping",
                "quality_score": 0.83,
                "local_score": 698.2,
                "section_title": "Hardware and layout",
                "table_title": None,
                "summary": "terminal_mapping evidence",
                "sample_texts": ["Page 863. Pin: P1.00 XL1.", "Page 863. Pin: P1.01 XL2."],
            },
            {
                "group_id": "doc:866:0",
                "evidence_family": "terminal_mapping",
                "quality_score": 0.83,
                "local_score": 698.2,
                "section_title": "Hardware and layout",
                "table_title": None,
                "summary": "terminal_mapping evidence",
                "sample_texts": ["Page 866. Pin: XC1.", "Page 866. Pin: XC2."],
            },
        ],
        api_key="test-key",
        model="gpt-test",
    )

    assert payload["ranked_group_ids"] == ["doc:863:0", "doc:866:0"]
    assert captured["model"] == "gpt-test"
    schema = captured["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])


def test_finalize_query_groups_broadens_selection_for_subquestion_coverage() -> None:
    planner_result = query_planner.QueryPlannerResult(
        mode="openai",
        spec=query_planner.QueryRetrievalSpec(
            intent="feature_to_terminal",
            primary_subject="LF/HF crystal pins",
            must_include_terms=["lf crystal", "hfxo"],
            should_include_terms=["xl1", "xl2", "xc1", "xc2"],
            identifier_terms=["XL1", "XL2", "XC1", "XC2", "LFXO", "HFXO"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            section_hints=["clock pins"],
            negative_terms=[],
            evidence_goal="find crystal pin rows",
            subquestions=["find lf crystal pins", "find hf crystal pins"],
        ),
        note="OpenAI query planner used.",
    )
    rerank_result = query_reranker.QueryGroupRerankResult(
        mode="openai",
        ranked_group_ids=["doc:863:0", "doc:869:0", "doc:866:0"],
        reason_codes={
            "doc:863:0": "LF_CRYSTAL_PIN_MAPPING",
            "doc:869:0": "LF_CRYSTAL_PIN_MAPPING",
            "doc:866:0": "HF_CRYSTAL_PIN_MAPPING",
        },
        note="OpenAI group reranker used.",
    )

    def make_row(page: int, text: str, display: str) -> storage.SearchResult:
        return storage.SearchResult(
            document_id="doc",
            page_number=page,
            chunk_index=0,
            chunk_type="table_row",
            source_text=text,
            bm25_score=0.0,
            score=325.0,
            table_index=0,
            row_index=0,
            section_title="Hardware and layout",
            headers=["Pin", "Name", "Function", "Description"],
            cells=[],
            entity_family="signal_or_terminal",
            entity_display_text=display,
        )

    lf_group_a = storage.QueryGroupCandidate(
        group_id="doc:863:0",
        document_id="doc",
        page_number=863,
        table_index=0,
        section_title="Hardware and layout",
        table_title=None,
        evidence_family="terminal_mapping",
        local_score=698.2,
        quality_score=0.83,
        summary="lf",
        items=[make_row(863, "Pin: 1. Name: P1.00 XL1. Description: Connection for 32.768 kHz crystal.", "P1.00")],
        sample_texts=["P1.00 XL1"],
    )
    lf_group_b = storage.QueryGroupCandidate(
        group_id="doc:869:0",
        document_id="doc",
        page_number=869,
        table_index=0,
        section_title="Hardware and layout",
        table_title=None,
        evidence_family="terminal_mapping",
        local_score=697.0,
        quality_score=0.83,
        summary="lf duplicate",
        items=[make_row(869, "Pin: 1. Name: P1.00 XL1. Description: Connection for 32.768 kHz crystal.", "P1.00")],
        sample_texts=["P1.00 XL1"],
    )
    hf_group = storage.QueryGroupCandidate(
        group_id="doc:866:0",
        document_id="doc",
        page_number=866,
        table_index=0,
        section_title="Hardware and layout",
        table_title=None,
        evidence_family="terminal_mapping",
        local_score=690.0,
        quality_score=0.83,
        summary="hf",
        items=[make_row(866, "Pin: 29. Name: XC1. Description: Connection for 32 MHz crystal.", "XC1")],
        sample_texts=["XC1"],
    )

    groups = storage._finalize_query_groups(
        question="which pins are meant for the LF and HF crystals?",
        planner_result=planner_result,
        candidate_groups=[lf_group_a, lf_group_b, hf_group],
        rerank_result=rerank_result,
        limit=2,
    )

    assert len(groups) == 2
    assert groups[0]["page_number"] == 863
    assert {group["page_number"] for group in groups} == {863, 866}
