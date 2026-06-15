"""OpenAI-assisted and local fallback query planning for evidence retrieval."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

VALID_INTENTS = {
    "terminal_lookup",
    "feature_to_terminal",
    "variant_package",
    "register_control",
    "spec_lookup",
    "generic",
}
VALID_TABLE_FAMILIES = {
    "pin_table",
    "register_table",
    "variant_table",
    "spec_table",
    "generic_table",
}
VALID_EVIDENCE_FAMILIES = {
    "terminal_mapping",
    "package_variant",
    "electrical_spec",
    "timing_spec",
    "feature_summary",
    "control_definition",
    "ordering_info",
    "mechanical_info",
    "application_circuit",
    "bom_like",
    "generic_text",
}
TABLE_FAMILY_ALIASES = {
    "pin assignments": "pin_table",
    "pin assignment table": "pin_table",
    "pin table": "pin_table",
    "terminal table": "pin_table",
    "terminal_lookup_table": "pin_table",
    "signal table": "pin_table",
    "register block": "register_table",
    "register blocks": "register_table",
    "register fields": "register_table",
    "register table": "register_table",
    "control table": "register_table",
    "variant table": "variant_table",
    "package table": "variant_table",
    "ordering table": "variant_table",
    "ordering information": "variant_table",
    "spec table": "spec_table",
    "specification table": "spec_table",
    "electrical table": "spec_table",
    "timing table": "spec_table",
    "generic text table": "generic_table",
    "generic table": "generic_table",
}
EVIDENCE_FAMILY_ALIASES = {
    "pin_table": "terminal_mapping",
    "pin table": "terminal_mapping",
    "pin assignments": "terminal_mapping",
    "pin assignment table": "terminal_mapping",
    "terminal table": "terminal_mapping",
    "terminal mapping": "terminal_mapping",
    "package table": "package_variant",
    "package variant": "package_variant",
    "variant table": "package_variant",
    "ordering table": "ordering_info",
    "ordering information": "ordering_info",
    "spec table": "electrical_spec",
    "electrical table": "electrical_spec",
    "electrical characteristics": "electrical_spec",
    "timing table": "timing_spec",
    "timing characteristics": "timing_spec",
    "feature table": "feature_summary",
    "truth table": "feature_summary",
    "mode table": "feature_summary",
    "register table": "control_definition",
    "register block": "control_definition",
    "control table": "control_definition",
    "mechanical drawing": "mechanical_info",
    "package dimensions": "mechanical_info",
    "application circuit": "application_circuit",
    "reference design": "application_circuit",
    "bill of materials": "bom_like",
    "bom": "bom_like",
    "generic table": "generic_text",
    "generic text": "generic_text",
}
INTENT_ALIASES = {
    "pin_lookup": "terminal_lookup",
    "pin lookup": "terminal_lookup",
    "terminal lookup": "terminal_lookup",
    "feature lookup": "generic",
    "feature_to_pin": "feature_to_terminal",
    "feature to pin": "feature_to_terminal",
    "package_lookup": "variant_package",
    "package lookup": "variant_package",
    "variant lookup": "variant_package",
    "register lookup": "register_control",
    "control lookup": "register_control",
    "spec lookup": "spec_lookup",
    "specification lookup": "spec_lookup",
}
PLANNER_SCHEMA_NAME = "query_retrieval_spec"

CRYSTAL_TERMS = {
    "crystal",
    "crystals",
    "oscillator",
    "oscillators",
    "clock",
    "clocking",
    "lfxo",
    "hfxo",
    "xl1",
    "xl2",
    "xc1",
    "xc2",
    "xl",
    "xc",
}
FEATURE_SIGNAL_EXPANSIONS = {
    "qspi": ["qspi", "qspi d0", "qspi d1", "qspi d2", "qspi d3", "qspi csn", "qspi sck", "d0", "d1", "d2", "d3", "csn", "sck"],
    "uarte": ["uarte", "uart", "txd", "rxd"],
    "uart": ["uart", "uarte", "txd", "rxd"],
    "spi": ["spi", "mosi", "miso", "sck", "csn"],
    "i2c": ["i2c", "twi", "sda", "scl"],
    "twi": ["twi", "i2c", "sda", "scl"],
}
DEFAULT_NEGATIVE_TERMS = [
    "package variants",
    "gpio pins",
    "wakeup pins",
    "analog input pins",
]
PLANNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": sorted(VALID_INTENTS)},
        "primary_subject": {"type": "string"},
        "must_include_terms": {"type": "array", "items": {"type": "string"}},
        "should_include_terms": {"type": "array", "items": {"type": "string"}},
        "identifier_terms": {"type": "array", "items": {"type": "string"}},
        "table_family_preferences": {"type": "array", "items": {"type": "string"}},
        "preferred_evidence_families": {"type": "array", "items": {"type": "string"}},
        "section_hints": {"type": "array", "items": {"type": "string"}},
        "negative_terms": {"type": "array", "items": {"type": "string"}},
        "evidence_goal": {"type": "string"},
        "subquestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "intent",
        "primary_subject",
        "must_include_terms",
        "should_include_terms",
        "identifier_terms",
        "table_family_preferences",
        "preferred_evidence_families",
        "section_hints",
        "negative_terms",
        "evidence_goal",
        "subquestions",
    ],
}
PLANNER_SYSTEM_PROMPT = """You plan evidence retrieval for electronics datasheets.
Return only JSON matching the provided schema.
Your job is query expansion and retrieval planning, not answering the question.
Prefer concise technical retrieval concepts.
Expand oscillator and crystal questions into relevant aliases and clock-pin terminology when appropriate.
Prefer pin assignment and clock-pin tables for pin-mapping questions.
Use only these table family values when applicable: pin_table, register_table, variant_table, spec_table, generic_table.
Include negative terms for obvious distractors like package-count tables when they are irrelevant."""


@dataclass(slots=True)
class QueryRetrievalSpec:
    """Structured retrieval plan consumed by local evidence assembly."""

    intent: str
    primary_subject: str
    must_include_terms: list[str]
    should_include_terms: list[str]
    identifier_terms: list[str]
    table_family_preferences: list[str]
    preferred_evidence_families: list[str]
    section_hints: list[str]
    negative_terms: list[str]
    evidence_goal: str
    subquestions: list[str]


@dataclass(slots=True)
class QueryPlannerResult:
    """Planner mode plus the validated retrieval spec."""

    mode: str
    spec: QueryRetrievalSpec
    note: str


def plan_query(question: str) -> QueryPlannerResult:
    """Plan a user question into a retrieval spec using OpenAI or a local fallback."""

    local_spec = build_local_retrieval_spec(question)
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("DATASHEET_RAG_QUERY_MODEL")
    if api_key and model:
        try:
            payload = _plan_query_via_openai(question=question, api_key=api_key, model=model)
            spec = _validate_retrieval_spec(payload, question=question)
            spec = _merge_query_specs(primary=spec, fallback=local_spec)
            return QueryPlannerResult(
                mode="openai",
                spec=spec,
                note="OpenAI query planner used and enriched with local heuristic expansions.",
            )
        except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
            return QueryPlannerResult(
                mode="local_fallback",
                spec=local_spec,
                note=f"OpenAI planner failed ({type(exc).__name__}: {exc}); local fallback used.",
            )

    reasons = []
    if not api_key:
        reasons.append("OPENAI_API_KEY missing")
    if not model:
        reasons.append("DATASHEET_RAG_QUERY_MODEL missing")
    return QueryPlannerResult(
        mode="local_fallback",
        spec=local_spec,
        note=f"OpenAI planner not configured ({'; '.join(reasons)}); local fallback used.",
    )


def build_local_retrieval_spec(question: str) -> QueryRetrievalSpec:
    """Build a deterministic fallback retrieval spec when the OpenAI planner is unavailable."""

    lower = question.lower()
    identifier_terms = _extract_identifier_terms(question)
    if any(term in lower for term in CRYSTAL_TERMS):
        want_lf = any(term in lower for term in ("lf", "lfxo", "low frequency", "32.768", "xl1", "xl2"))
        want_hf = any(term in lower for term in ("hf", "hfxo", "high frequency", "xc1", "xc2"))
        must_include_terms = []
        should_include_terms = ["clock pins", "pin assignments", "dedicated pins"]
        if want_lf or "crystal" in lower:
            must_include_terms.extend(["lf crystal", "lfxo"])
            should_include_terms.extend(["32.768 kHz", "xl1", "xl2", "low frequency crystal"])
        if want_hf or "crystal" in lower:
            must_include_terms.extend(["hf crystal", "hfxo"])
            should_include_terms.extend(["xc1", "xc2", "high frequency crystal", "high frequency clock"])
        must_include_terms = _unique_clean(must_include_terms)
        should_include_terms = _unique_clean([*should_include_terms, *identifier_terms])
        identifier_terms = _unique_clean(
            [*identifier_terms, "XL1", "XL2", "XC1", "XC2", "LFXO", "HFXO"],
            lowercase=False,
        )
        return QueryRetrievalSpec(
            intent="feature_to_terminal",
            primary_subject="LF/HF crystal pins",
            must_include_terms=must_include_terms,
            should_include_terms=should_include_terms,
            identifier_terms=identifier_terms,
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping", "feature_summary", "application_circuit"],
            section_hints=["clock pins", "pin assignments", "dedicated pins"],
            negative_terms=list(DEFAULT_NEGATIVE_TERMS),
            evidence_goal="find all pin rows for LF/HF crystal signals",
            subquestions=["find LF crystal pins", "find HF crystal pins"],
        )

    for feature, expansions in FEATURE_SIGNAL_EXPANSIONS.items():
        if feature in lower:
            pin_or_mapping_request = any(term in lower for term in ("pin", "pins", "canonical", "mapped", "mapping", "connected to"))
            return QueryRetrievalSpec(
                intent="feature_to_terminal" if pin_or_mapping_request else "generic",
                primary_subject=feature.upper(),
                must_include_terms=_unique_clean([feature]),
                should_include_terms=_unique_clean(
                    [
                        *expansions,
                        "pin assignments",
                        "overview",
                        "operation",
                        "configuration",
                        "registers",
                        "description",
                        *identifier_terms,
                    ]
                ),
                identifier_terms=_unique_clean(identifier_terms, lowercase=False),
                table_family_preferences=["pin_table"] if pin_or_mapping_request else ["generic_table", "register_table", "pin_table"],
                preferred_evidence_families=(
                    ["terminal_mapping", "feature_summary"]
                    if pin_or_mapping_request
                    else ["feature_summary", "control_definition", "generic_text", "terminal_mapping"]
                ),
                section_hints=(
                    ["pin assignments", "clock pins"]
                    if pin_or_mapping_request
                    else ["overview", "operation", "configuration", "registers", "pin configuration"]
                ),
                negative_terms=list(DEFAULT_NEGATIVE_TERMS if pin_or_mapping_request else ["package variants", "mechanical drawing", "bill of materials"]),
                evidence_goal=(
                    f"find pin rows for {feature.upper()} signals"
                    if pin_or_mapping_request
                    else f"find explanatory prose and structured feature/control evidence for how the {feature.upper()} function works"
                ),
                subquestions=[],
            )

    if any(term in lower for term in ("package", "variant", "part number", "ordering")):
        return QueryRetrievalSpec(
            intent="variant_package",
            primary_subject="package or variant lookup",
            must_include_terms=[],
            should_include_terms=_unique_clean(["part number", "package", "ordering information", *identifier_terms]),
            identifier_terms=_unique_clean(identifier_terms, lowercase=False),
            table_family_preferences=["variant_table"],
            preferred_evidence_families=["package_variant", "ordering_info"],
            section_hints=["product variants", "ordering information"],
            negative_terms=[],
            evidence_goal="find ordering or package table rows",
            subquestions=[],
        )

    if identifier_terms:
        return QueryRetrievalSpec(
            intent="terminal_lookup",
            primary_subject=identifier_terms[0],
            must_include_terms=[],
            should_include_terms=_unique_clean(["function", "description", "pin assignments", *identifier_terms]),
            identifier_terms=_unique_clean(identifier_terms, lowercase=False),
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            section_hints=["pin assignments"],
            negative_terms=list(DEFAULT_NEGATIVE_TERMS),
            evidence_goal=f"find structured pin rows for {identifier_terms[0]}",
            subquestions=[],
        )

    if any(term in lower for term in ("register", "field", "address offset", "control", "subscribe", "task")):
        return QueryRetrievalSpec(
            intent="register_control",
            primary_subject="register or control item",
            must_include_terms=[],
            should_include_terms=_unique_clean(["register", "field", "description", *identifier_terms]),
            identifier_terms=_unique_clean(identifier_terms, lowercase=False),
            table_family_preferences=["register_table"],
            preferred_evidence_families=["control_definition"],
            section_hints=["registers"],
            negative_terms=[],
            evidence_goal="find register field rows",
            subquestions=[],
        )

    if any(term in lower for term in ("mode", "modes", "truth table", "state", "states", "high-impedance")):
        return QueryRetrievalSpec(
            intent="generic",
            primary_subject="operating modes or truth table",
            must_include_terms=[],
            should_include_terms=_unique_clean(["mode", "state", "truth table", "input", "output", *identifier_terms]),
            identifier_terms=_unique_clean(identifier_terms, lowercase=False),
            table_family_preferences=["generic_table"],
            preferred_evidence_families=["feature_summary"],
            section_hints=["operating modes", "truth table"],
            negative_terms=["mechanical drawing", "package dimensions"],
            evidence_goal="find feature-summary rows that describe modes or truth-table behavior",
            subquestions=[],
        )

    if any(term in lower for term in ("voltage", "current", "timing", "frequency", "accuracy", "temperature", "typ", "max", "min")):
        return QueryRetrievalSpec(
            intent="spec_lookup",
            primary_subject="electrical or timing specification",
            must_include_terms=[],
            should_include_terms=_unique_clean(["parameter", "min", "typ", "max", "unit", *identifier_terms]),
            identifier_terms=_unique_clean(identifier_terms, lowercase=False),
            table_family_preferences=["spec_table"],
            preferred_evidence_families=["electrical_spec", "timing_spec"],
            section_hints=["electrical characteristics"],
            negative_terms=[],
            evidence_goal="find structured specification rows",
            subquestions=[],
        )

    return QueryRetrievalSpec(
        intent="generic",
        primary_subject=_clean_text(question)[:80],
        must_include_terms=[],
        should_include_terms=_unique_clean([*identifier_terms]),
        identifier_terms=_unique_clean(identifier_terms, lowercase=False),
        table_family_preferences=["generic_table"],
        preferred_evidence_families=["generic_text", "feature_summary"],
        section_hints=[],
        negative_terms=[],
        evidence_goal="find the most relevant evidence rows or chunks",
        subquestions=[],
    )


def _plan_query_via_openai(*, question: str, api_key: str, model: str) -> dict[str, object]:
    """Use the OpenAI Responses API to plan the query into strict JSON."""

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": PLANNER_SCHEMA_NAME,
                "strict": True,
                "schema": PLANNER_SCHEMA,
            }
        },
        max_output_tokens=700,
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
                raise RuntimeError(f"Planner refusal: {refusal}")
    raise RuntimeError("Planner returned no usable text output.")


def _validate_retrieval_spec(payload: dict[str, object], *, question: str) -> QueryRetrievalSpec:
    """Validate planner JSON and normalize it into a retrieval spec."""

    if not isinstance(payload, dict):
        raise ValueError("Planner payload must be a JSON object.")

    intent = _normalize_intent(_require_string(payload, "intent")).strip().lower()
    if intent not in VALID_INTENTS:
        raise ValueError(f"Unsupported intent: {intent}")

    table_family_preferences = _normalize_table_family_list(
        payload.get("table_family_preferences", []),
        intent=intent,
    )
    preferred_evidence_families = _normalize_evidence_family_list(
        payload.get("preferred_evidence_families", []),
        intent=intent,
        table_family_preferences=table_family_preferences,
    )
    primary_subject = _optional_string(payload, "primary_subject") or _default_primary_subject(
        intent=intent,
        question=question,
    )
    evidence_goal = _optional_string(payload, "evidence_goal") or _default_evidence_goal(intent)

    return QueryRetrievalSpec(
        intent=intent,
        primary_subject=primary_subject,
        must_include_terms=_normalize_string_list(payload.get("must_include_terms", []), allow_scalar=True),
        should_include_terms=_normalize_string_list(payload.get("should_include_terms", []), allow_scalar=True),
        identifier_terms=_normalize_identifier_list(payload.get("identifier_terms", []), allow_scalar=True),
        table_family_preferences=table_family_preferences,
        preferred_evidence_families=preferred_evidence_families,
        section_hints=_normalize_string_list(payload.get("section_hints", []), allow_scalar=True),
        negative_terms=_normalize_string_list(payload.get("negative_terms", []), allow_scalar=True),
        evidence_goal=evidence_goal,
        subquestions=_normalize_string_list(payload.get("subquestions", []), allow_scalar=True),
    )


def _require_string(payload: dict[str, object], key: str) -> str:
    """Read a required string field from planner JSON."""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Planner field '{key}' must be a non-empty string.")
    return _clean_text(value)


def _optional_string(payload: dict[str, object], key: str) -> str:
    """Read an optional string field from planner JSON."""

    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Planner field '{key}' must be a string when present.")
    return _clean_text(value)


def _normalize_intent(value: str) -> str:
    """Map planner intent aliases onto the supported local intent set."""

    normalized = _clean_text(value).lower().replace("-", "_")
    normalized = normalized.replace(" ", "_")
    return INTENT_ALIASES.get(normalized, normalized)


def _normalize_string_list(values: object, *, allow_scalar: bool = False) -> list[str]:
    """Normalize a planner list of general retrieval terms."""

    if allow_scalar and isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError("Planner list field must be an array.")
    normalized = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Planner list items must be strings.")
        cleaned = _clean_text(value)
        if cleaned:
            normalized.append(cleaned.lower())
    return _unique_clean(normalized)


def _normalize_identifier_list(values: object, *, allow_scalar: bool = False) -> list[str]:
    """Normalize a planner list of exact identifiers while preserving case."""

    if allow_scalar and isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ValueError("Planner identifier field must be an array.")
    normalized = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Planner identifier items must be strings.")
        cleaned = _clean_text(value)
        if cleaned:
            normalized.append(cleaned)
    return _unique_clean(normalized, lowercase=False)


def _normalize_table_family_list(values: object, *, intent: str) -> list[str]:
    """Normalize planner table-family preferences, accepting common aliases."""

    normalized_families = _normalize_string_list(values, allow_scalar=True)
    accepted: list[str] = []
    for family in normalized_families:
        canonical = TABLE_FAMILY_ALIASES.get(family, family)
        if canonical in VALID_TABLE_FAMILIES:
            accepted.append(canonical)
    unique = _unique_clean(accepted)
    if unique:
        return unique
    return _default_table_families_for_intent(intent)


def _normalize_evidence_family_list(
    values: object,
    *,
    intent: str,
    table_family_preferences: list[str],
) -> list[str]:
    """Normalize generic evidence-family preferences and derive defaults."""

    normalized_families = _normalize_string_list(values, allow_scalar=True)
    accepted: list[str] = []
    for family in normalized_families:
        canonical = EVIDENCE_FAMILY_ALIASES.get(family, family)
        if canonical in VALID_EVIDENCE_FAMILIES:
            accepted.append(canonical)
    unique = _unique_clean(accepted)
    if unique:
        return unique
    derived: list[str] = []
    for family in table_family_preferences:
        mapped = EVIDENCE_FAMILY_ALIASES.get(family)
        if mapped:
            derived.append(mapped)
    if derived:
        return _unique_clean(derived)
    return _default_evidence_families_for_intent(intent)


def _default_table_families_for_intent(intent: str) -> list[str]:
    """Choose a safe default table-family preference when the planner is vague."""

    if intent in {"terminal_lookup", "feature_to_terminal"}:
        return ["pin_table"]
    if intent == "variant_package":
        return ["variant_table"]
    if intent == "register_control":
        return ["register_table"]
    if intent == "spec_lookup":
        return ["spec_table"]
    return ["generic_table"]


def _default_evidence_families_for_intent(intent: str) -> list[str]:
    """Choose default generic evidence-family preferences when the planner is vague."""

    if intent == "feature_to_terminal":
        return ["terminal_mapping", "feature_summary", "application_circuit"]
    if intent == "terminal_lookup":
        return ["terminal_mapping"]
    if intent == "variant_package":
        return ["package_variant", "ordering_info"]
    if intent == "register_control":
        return ["control_definition"]
    if intent == "spec_lookup":
        return ["electrical_spec", "timing_spec"]
    return ["generic_text", "feature_summary"]


def _default_primary_subject(*, intent: str, question: str) -> str:
    """Derive a fallback primary subject when the planner omits one."""

    cleaned_question = _clean_text(question)
    if intent == "feature_to_terminal":
        return "feature-to-terminal lookup"
    if intent == "terminal_lookup":
        identifiers = _extract_identifier_terms(question)
        if identifiers:
            return identifiers[0]
        return "terminal lookup"
    if intent == "variant_package":
        return "package or variant lookup"
    if intent == "register_control":
        return "register or control item"
    if intent == "spec_lookup":
        return "electrical or timing specification"
    return cleaned_question[:80] or "generic lookup"


def _default_evidence_goal(intent: str) -> str:
    """Derive a fallback evidence goal when the planner omits one."""

    if intent == "feature_to_terminal":
        return "find pin rows for the requested feature signals"
    if intent == "terminal_lookup":
        return "find structured pin or terminal rows"
    if intent == "variant_package":
        return "find ordering or package table rows"
    if intent == "register_control":
        return "find register field rows"
    if intent == "spec_lookup":
        return "find structured specification rows"
    return "find the most relevant evidence rows or chunks"


def _merge_query_specs(*, primary: QueryRetrievalSpec, fallback: QueryRetrievalSpec) -> QueryRetrievalSpec:
    """Merge an OpenAI planner spec with the deterministic local heuristic spec."""

    intent = _prefer_intent(primary.intent, fallback.intent)
    table_family_preferences = _merge_priority_list(
        primary.table_family_preferences,
        fallback.table_family_preferences,
    ) or _default_table_families_for_intent(intent)
    preferred_evidence_families = _merge_priority_list(
        primary.preferred_evidence_families,
        fallback.preferred_evidence_families,
    ) or _default_evidence_families_for_intent(intent)
    identifier_terms = _merge_priority_list(
        primary.identifier_terms,
        fallback.identifier_terms,
        lowercase=False,
    )
    must_include_terms = _merge_priority_list(
        primary.must_include_terms,
        fallback.must_include_terms,
    )
    should_include_terms = _merge_priority_list(
        primary.should_include_terms,
        fallback.should_include_terms,
    )
    section_hints = _merge_priority_list(
        primary.section_hints,
        fallback.section_hints,
    )
    negative_terms = _merge_priority_list(
        primary.negative_terms,
        fallback.negative_terms,
    )
    subquestions = _merge_priority_list(
        primary.subquestions,
        fallback.subquestions,
    )
    primary_subject = primary.primary_subject or fallback.primary_subject
    evidence_goal = primary.evidence_goal or fallback.evidence_goal

    return QueryRetrievalSpec(
        intent=intent,
        primary_subject=primary_subject,
        must_include_terms=must_include_terms,
        should_include_terms=should_include_terms,
        identifier_terms=identifier_terms,
        table_family_preferences=table_family_preferences,
        preferred_evidence_families=preferred_evidence_families,
        section_hints=section_hints,
        negative_terms=negative_terms,
        evidence_goal=evidence_goal,
        subquestions=subquestions,
    )


def _prefer_intent(primary_intent: str, fallback_intent: str) -> str:
    """Prefer the more specific intent when planner and heuristic differ."""

    if primary_intent == fallback_intent:
        return primary_intent
    specificity = {
        "feature_to_terminal": 5,
        "terminal_lookup": 4,
        "variant_package": 4,
        "register_control": 4,
        "spec_lookup": 4,
        "generic": 1,
    }
    if specificity.get(fallback_intent, 0) > specificity.get(primary_intent, 0):
        return fallback_intent
    return primary_intent


def _merge_priority_list(
    primary_values: list[str],
    fallback_values: list[str],
    *,
    lowercase: bool = True,
) -> list[str]:
    """Merge planner and heuristic lists while preserving order and specificity."""

    merged: list[str] = []
    merged.extend(primary_values)
    for value in fallback_values:
        if value not in merged:
            merged.append(value)
    return _unique_clean(merged, lowercase=lowercase)


def _extract_identifier_terms(question: str) -> list[str]:
    """Extract likely technical identifiers from the raw user query."""

    matches = re.findall(r"[A-Za-z]+\d+(?:[./][A-Za-z0-9]+)?|[A-Za-z]{2,}\d*[A-Za-z0-9]*", question)
    candidates = [
        match
        for match in matches
        if any(character.isdigit() for character in match) or match.upper() == match
    ]
    return _unique_clean(candidates, lowercase=False)


def _unique_clean(values: list[str], *, lowercase: bool = True) -> list[str]:
    """Deduplicate and normalize strings while preserving order."""

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower() if lowercase else cleaned
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned.lower() if lowercase else cleaned)
    return unique


def _clean_text(text: str) -> str:
    """Normalize whitespace in planner strings."""

    return re.sub(r"\s+", " ", text).strip()
