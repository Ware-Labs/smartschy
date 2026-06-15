"""Generic datasheet entity extraction and query-evidence helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from datasheet_rag.chunking import ChunkRecord
from datasheet_rag.table_extraction import ExtractedTable, ExtractedTableRow

ENTITY_PARSER_VERSION = "generic-electronics-core-v1"

TERMINAL_HEADERS = {"pin", "ball", "pad", "terminal", "lead", "contact", "port"}
NAME_HEADERS = {"name", "signal", "terminal name", "pin name", "ball name", "pad name"}
FUNCTION_HEADERS = {"function", "type", "mode", "usage"}
DESCRIPTION_HEADERS = {"description", "notes", "comment"}
VARIANT_HEADERS = {
    "part number",
    "variant",
    "package",
    "ordering information",
    "order code",
    "temperature grade",
    "memory",
    "nvm",
    "ram",
}
REGISTER_HEADERS = {"id", "r/w", "field", "value id", "value", "description"}
SPEC_HEADERS = {
    "parameter",
    "conditions",
    "condition",
    "min",
    "typ",
    "max",
    "unit",
    "rating",
    "value",
}
FEATURE_KEYWORDS = {
    "gpio",
    "uart",
    "uarte",
    "spi",
    "i2c",
    "twi",
    "adc",
    "dac",
    "pwm",
    "qspi",
    "timer",
    "radio",
    "ble",
    "bluetooth",
    "pll",
    "osc",
    "xosc",
    "dppi",
    "shutdown",
    "enable",
    "protection",
    "sensing",
    "analog input",
    "digital i/o",
}


@dataclass(slots=True)
class EntityRecord:
    """One normalized entity extracted from structured evidence."""

    entity_key: str
    entity_family: str
    normalized_key: str
    display_text: str
    raw_text: str
    aliases: list[str]
    confidence: float
    metadata: dict[str, object]


@dataclass(slots=True)
class EntityEvidenceRecord:
    """Provenance for one entity mention."""

    entity_key: str
    page_number: int
    source_kind: str
    evidence_text: str
    confidence: float
    table_index: int | None = None
    row_index: int | None = None
    chunk_index: int | None = None
    metadata: dict[str, object] | None = None


@dataclass(slots=True)
class EntityRelationRecord:
    """Directed relation between two extracted entities."""

    source_entity_key: str
    relation_type: str
    target_entity_key: str
    confidence: float
    metadata: dict[str, object]


@dataclass(slots=True)
class EntityExtractionResult:
    """All entity artifacts generated for one document."""

    entities: list[EntityRecord]
    evidence: list[EntityEvidenceRecord]
    relations: list[EntityRelationRecord]


@dataclass(slots=True)
class QueryResponse:
    """Evidence-pack payload for the CLI query command."""

    question: str
    intent: str
    planner_mode: str
    rerank_mode: str
    primary_subject: str
    must_include_terms: list[str]
    should_include_terms: list[str]
    identifier_terms: list[str]
    table_family_preferences: list[str]
    preferred_evidence_families: list[str]
    subquestions: list[str]
    section_hints: list[str]
    negative_terms: list[str]
    retrieval_summary: str
    candidate_family_summary: dict[str, int]
    structured_evidence_groups: list[dict[str, object]]
    prose_evidence_groups: list[dict[str, object]]
    coverage_notes: list[str]

    @property
    def evidence_groups(self) -> list[dict[str, object]]:
        """Compatibility accessor returning both evidence channels together."""

        return [*self.structured_evidence_groups, *self.prose_evidence_groups]


@dataclass(slots=True)
class EvalCaseResult:
    """Outcome for one evaluation case."""

    query: str
    first_relevant_rank: int | None
    hit_at_1: bool
    hit_at_3: bool
    hit_at_5: bool
    family_match: bool | None
    evidence_match: bool | None
    planner_trace_match: bool | None


def extract_document_entities(
    *,
    document_id: str,
    source_path: Path,
    metadata: dict[str, object],
    chunks: list[ChunkRecord],
    tables: list[ExtractedTable],
    table_rows: list[ExtractedTableRow],
) -> EntityExtractionResult:
    """Extract generic datasheet entities from already-ingested artifacts."""

    registry = _EntityRegistry(document_id=document_id)
    component_keys = _extract_component_entities(
        registry=registry,
        source_path=source_path,
        metadata=metadata,
        chunks=chunks,
    )
    _extract_table_and_section_entities(
        registry=registry,
        tables=tables,
    )

    tables_by_key = {(table.page_number, table.table_index): table for table in tables}
    grouped_rows: dict[tuple[int, int], list[ExtractedTableRow]] = {}
    for row in table_rows:
        grouped_rows.setdefault((row.page_number, row.table_index), []).append(row)

    for key, rows in grouped_rows.items():
        rows.sort(key=lambda item: item.row_index)
        table = tables_by_key.get(key)
        if table is None:
            continue
        headers = _normalize_headers(rows[0].headers if rows else table.headers)
        table_family = _classify_table_family(headers=headers, table_kind=table.table_kind)
        if table_family == "terminal_table":
            _extract_terminal_entities(
                registry=registry,
                component_keys=component_keys,
                table=table,
                rows=rows,
                headers=headers,
            )
        elif table_family == "variant_table":
            _extract_variant_entities(
                registry=registry,
                component_keys=component_keys,
                table=table,
                rows=rows,
                headers=headers,
            )
        elif table_family == "register_table":
            _extract_register_entities(
                registry=registry,
                table=table,
                rows=rows,
                headers=headers,
            )
        elif table_family == "spec_table":
            _extract_spec_entities(
                registry=registry,
                component_keys=component_keys,
                table=table,
                rows=rows,
                headers=headers,
            )
        else:
            _extract_generic_feature_entities(
                registry=registry,
                component_keys=component_keys,
                table=table,
                rows=rows,
                headers=headers,
            )

    _apply_mcu_enricher(registry)
    return EntityExtractionResult(
        entities=list(registry.entities.values()),
        evidence=list(registry.evidence),
        relations=list(registry.relations),
    )


def normalize_entity_key(text: str) -> str:
    """Build a compact normalized key that tolerates datasheet punctuation."""

    compact = "".join(re.findall(r"[A-Za-z0-9]+", text)).lower()
    return compact or "entity"


def evaluate_cases(
    *,
    cases: list[dict[str, object]],
    search_fn: callable,
    query_fn: callable,
) -> dict[str, object]:
    """Run a compact evaluation over search and query-evidence behavior."""

    results: list[EvalCaseResult] = []
    for case in cases:
        query_text = str(case["query"])
        search_results = search_fn(query_text, 5)
        query_response = query_fn(query_text)
        first_rank = _first_relevant_rank(search_results, case)
        family_match = _family_match(search_results, case)
        evidence_match = _evidence_match(query_response, case)
        planner_trace_match = _planner_trace_match(query_response, case)
        results.append(
            EvalCaseResult(
                query=query_text,
                first_relevant_rank=first_rank,
                hit_at_1=first_rank == 1,
                hit_at_3=first_rank is not None and first_rank <= 3,
                hit_at_5=first_rank is not None and first_rank <= 5,
                family_match=family_match,
                evidence_match=evidence_match,
                planner_trace_match=planner_trace_match,
            )
        )

    total = len(results) or 1
    relevant_ranks = [item.first_relevant_rank for item in results if item.first_relevant_rank is not None]
    family_values = [item.family_match for item in results if item.family_match is not None]
    evidence_values = [item.evidence_match for item in results if item.evidence_match is not None]
    planner_trace_values = [item.planner_trace_match for item in results if item.planner_trace_match is not None]

    return {
        "total_cases": len(results),
        "hit_at_1": sum(1 for item in results if item.hit_at_1),
        "hit_at_3": sum(1 for item in results if item.hit_at_3),
        "hit_at_5": sum(1 for item in results if item.hit_at_5),
        "hit_at_1_rate": round(sum(1 for item in results if item.hit_at_1) / total, 4),
        "hit_at_3_rate": round(sum(1 for item in results if item.hit_at_3) / total, 4),
        "hit_at_5_rate": round(sum(1 for item in results if item.hit_at_5) / total, 4),
        "mean_first_relevant_rank": (
            round(sum(relevant_ranks) / len(relevant_ranks), 4) if relevant_ranks else None
        ),
        "family_match_rate": (
            round(sum(1 for value in family_values if value) / len(family_values), 4)
            if family_values
            else None
        ),
        "evidence_match_rate": (
            round(sum(1 for value in evidence_values if value) / len(evidence_values), 4)
            if evidence_values
            else None
        ),
        "planner_trace_match_rate": (
            round(sum(1 for value in planner_trace_values if value) / len(planner_trace_values), 4)
            if planner_trace_values
            else None
        ),
        "cases": [
            {
                "query": item.query,
                "first_relevant_rank": item.first_relevant_rank,
                "hit_at_1": item.hit_at_1,
                "hit_at_3": item.hit_at_3,
                "hit_at_5": item.hit_at_5,
                "family_match": item.family_match,
                "evidence_match": item.evidence_match,
                "planner_trace_match": item.planner_trace_match,
            }
            for item in results
        ],
    }


def load_eval_cases(eval_file: Path) -> list[dict[str, object]]:
    """Load compact JSON evaluation cases from disk."""

    payload = json.loads(eval_file.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    cases = payload.get("cases")
    if isinstance(cases, list):
        return cases
    raise ValueError("Evaluation file must be a JSON list or an object with a 'cases' list.")


class _EntityRegistry:
    """Helper for stable entity, evidence, and relation creation."""

    def __init__(self, *, document_id: str) -> None:
        self.document_id = document_id
        self.entities: dict[str, EntityRecord] = {}
        self.evidence: list[EntityEvidenceRecord] = []
        self.relations: list[EntityRelationRecord] = []
        self._relation_keys: set[tuple[str, str, str]] = set()

    def add_entity(
        self,
        *,
        entity_family: str,
        display_text: str,
        raw_text: str | None = None,
        aliases: list[str] | None = None,
        confidence: float = 0.75,
        metadata: dict[str, object] | None = None,
    ) -> str:
        display = _clean_text(display_text)
        if not display:
            raise ValueError("display_text must be non-empty")
        key = f"{entity_family}:{normalize_entity_key(display)}"
        alias_values = _unique_strings([display, *(aliases or [])])
        if key not in self.entities:
            self.entities[key] = EntityRecord(
                entity_key=key,
                entity_family=entity_family,
                normalized_key=normalize_entity_key(display),
                display_text=display,
                raw_text=_clean_text(raw_text or display),
                aliases=alias_values,
                confidence=confidence,
                metadata=dict(metadata or {}),
            )
            return key

        record = self.entities[key]
        record.aliases = _unique_strings([*record.aliases, *alias_values])
        record.confidence = max(record.confidence, confidence)
        if metadata:
            record.metadata.update(metadata)
        if raw_text:
            record.raw_text = _clean_text(raw_text)
        return key

    def add_evidence(
        self,
        *,
        entity_key: str,
        page_number: int,
        source_kind: str,
        evidence_text: str,
        confidence: float = 0.75,
        table_index: int | None = None,
        row_index: int | None = None,
        chunk_index: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        text = _clean_text(evidence_text)
        if not text:
            return
        self.evidence.append(
            EntityEvidenceRecord(
                entity_key=entity_key,
                page_number=page_number,
                source_kind=source_kind,
                evidence_text=text,
                confidence=confidence,
                table_index=table_index,
                row_index=row_index,
                chunk_index=chunk_index,
                metadata=dict(metadata or {}),
            )
        )

    def add_relation(
        self,
        *,
        source_entity_key: str,
        relation_type: str,
        target_entity_key: str,
        confidence: float = 0.7,
        metadata: dict[str, object] | None = None,
    ) -> None:
        relation_key = (source_entity_key, relation_type, target_entity_key)
        if relation_key in self._relation_keys:
            return
        self._relation_keys.add(relation_key)
        self.relations.append(
            EntityRelationRecord(
                source_entity_key=source_entity_key,
                relation_type=relation_type,
                target_entity_key=target_entity_key,
                confidence=confidence,
                metadata=dict(metadata or {}),
            )
        )


def _extract_component_entities(
    *,
    registry: _EntityRegistry,
    source_path: Path,
    metadata: dict[str, object],
    chunks: list[ChunkRecord],
) -> list[str]:
    """Create one or more root component entities for the document."""

    component_keys: list[str] = []
    title = _clean_text(str(metadata.get("title", "")))
    first_chunk = chunks[0].source_text.splitlines()[0].strip() if chunks else ""
    candidates = [title, first_chunk, source_path.stem]
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _clean_text(candidate)
        if not cleaned:
            continue
        normalized = normalize_entity_key(cleaned)
        if normalized in seen:
            continue
        seen.add(normalized)
        key = registry.add_entity(
            entity_family="component",
            display_text=cleaned,
            raw_text=cleaned,
            aliases=_extract_identifier_aliases(cleaned),
            confidence=0.8 if candidate == title else 0.65,
            metadata={"source": "document_metadata"},
        )
        component_keys.append(key)
        registry.add_evidence(
            entity_key=key,
            page_number=1,
            source_kind="document_metadata",
            evidence_text=cleaned,
            confidence=0.8,
        )
    return component_keys


def _extract_table_and_section_entities(
    *,
    registry: _EntityRegistry,
    tables: list[ExtractedTable],
) -> None:
    """Create lightweight table and section objects for context-aware retrieval."""

    for table in tables:
        table_label = table.table_title or table.header_signature or f"table page {table.page_number}"
        table_key = registry.add_entity(
            entity_family="table_object",
            display_text=table_label,
            aliases=[table.table_kind, table.header_signature],
            confidence=0.6,
            metadata={
                "page_number": table.page_number,
                "table_index": table.table_index,
                "table_kind": table.table_kind,
            },
        )
        registry.add_evidence(
            entity_key=table_key,
            page_number=table.page_number,
            source_kind="table",
            evidence_text=f"{table.section_title or ''} {table_label}".strip(),
            confidence=0.6,
            table_index=table.table_index,
        )
        if table.section_title:
            section_key = registry.add_entity(
                entity_family="section_object",
                display_text=table.section_title,
                confidence=0.7,
                metadata={"page_number": table.page_number},
            )
            registry.add_evidence(
                entity_key=section_key,
                page_number=table.page_number,
                source_kind="section",
                evidence_text=table.section_title,
                confidence=0.7,
                table_index=table.table_index,
            )
            registry.add_relation(
                source_entity_key=table_key,
                relation_type="described_in",
                target_entity_key=section_key,
                confidence=0.7,
                metadata={"page_number": table.page_number},
            )


def _extract_terminal_entities(
    *,
    registry: _EntityRegistry,
    component_keys: list[str],
    table: ExtractedTable,
    rows: list[ExtractedTableRow],
    headers: list[str],
) -> None:
    """Extract generic terminal/pin/ball entities and function mappings."""

    name_index = _find_header_index(headers, NAME_HEADERS)
    terminal_index = _find_header_index(headers, TERMINAL_HEADERS)
    function_index = _find_header_index(headers, FUNCTION_HEADERS)
    description_index = _find_header_index(headers, DESCRIPTION_HEADERS)

    for row in rows:
        if row.row_type == "group_header":
            continue
        name_cell = _cell(row, name_index)
        terminal_cell = _cell(row, terminal_index)
        if not name_cell and not terminal_cell:
            continue

        aliases = _extract_identifier_aliases(name_cell or terminal_cell or "")
        display_text = aliases[0] if aliases else _clean_text(name_cell or terminal_cell or "")
        if not display_text:
            continue
        entity_key = registry.add_entity(
            entity_family="signal_or_terminal",
            display_text=display_text,
            raw_text=name_cell or terminal_cell or display_text,
            aliases=_unique_strings(
                aliases
                + [value for value in [name_cell, terminal_cell] if value]
            ),
            confidence=0.9,
            metadata={
                "table_kind": table.table_kind,
                "section_title": table.section_title,
            },
        )
        registry.add_evidence(
            entity_key=entity_key,
            page_number=row.page_number,
            source_kind="table_row",
            table_index=row.table_index,
            row_index=row.row_index,
            evidence_text=row.text_rendering,
            confidence=0.9,
        )
        for component_key in component_keys:
            registry.add_relation(
                source_entity_key=component_key,
                relation_type="has_terminal",
                target_entity_key=entity_key,
                confidence=0.85,
                metadata={"page_number": row.page_number},
            )

        feature_texts = _extract_feature_texts(
            _cell(row, function_index),
            _cell(row, description_index),
        )
        for feature_text in feature_texts:
            feature_key = registry.add_entity(
                entity_family="interface_or_feature",
                display_text=feature_text,
                aliases=_extract_identifier_aliases(feature_text),
                confidence=0.75,
                metadata={"source": "pin_table"},
            )
            registry.add_evidence(
                entity_key=feature_key,
                page_number=row.page_number,
                source_kind="table_row",
                table_index=row.table_index,
                row_index=row.row_index,
                evidence_text=row.text_rendering,
                confidence=0.7,
            )
            registry.add_relation(
                source_entity_key=entity_key,
                relation_type="maps_to_function",
                target_entity_key=feature_key,
                confidence=0.8,
                metadata={"page_number": row.page_number},
            )
            for component_key in component_keys:
                registry.add_relation(
                    source_entity_key=component_key,
                    relation_type="supports_feature",
                    target_entity_key=feature_key,
                    confidence=0.65,
                    metadata={"page_number": row.page_number},
                )


def _extract_variant_entities(
    *,
    registry: _EntityRegistry,
    component_keys: list[str],
    table: ExtractedTable,
    rows: list[ExtractedTableRow],
    headers: list[str],
) -> None:
    """Extract variant/package/ordering rows into generic component/spec entities."""

    part_index = _find_header_index(headers, {"part number", "variant", "order code", "ordering information"})
    if part_index is None:
        part_index = 0

    for row in rows:
        if row.row_type == "group_header":
            continue
        part_value = _cell(row, part_index)
        if not part_value:
            continue
        variant_key = registry.add_entity(
            entity_family="component",
            display_text=part_value,
            aliases=_extract_identifier_aliases(part_value),
            confidence=0.88,
            metadata={"source": "variant_table"},
        )
        registry.add_evidence(
            entity_key=variant_key,
            page_number=row.page_number,
            source_kind="table_row",
            table_index=row.table_index,
            row_index=row.row_index,
            evidence_text=row.text_rendering,
            confidence=0.88,
        )
        for component_key in component_keys:
            registry.add_relation(
                source_entity_key=component_key,
                relation_type="has_variant",
                target_entity_key=variant_key,
                confidence=0.85,
                metadata={"page_number": row.page_number},
            )

        for header, cell in zip(headers, row.cells):
            if not cell or header == headers[part_index]:
                continue
            spec_key = registry.add_entity(
                entity_family="spec_item",
                display_text=f"{header}: {cell}" if header else cell,
                raw_text=cell,
                aliases=[cell],
                confidence=0.72,
                metadata={"source": "variant_table"},
            )
            registry.add_evidence(
                entity_key=spec_key,
                page_number=row.page_number,
                source_kind="table_row",
                table_index=row.table_index,
                row_index=row.row_index,
                evidence_text=row.text_rendering,
                confidence=0.72,
            )
            registry.add_relation(
                source_entity_key=variant_key,
                relation_type="has_spec",
                target_entity_key=spec_key,
                confidence=0.72,
                metadata={"page_number": row.page_number},
            )


def _extract_register_entities(
    *,
    registry: _EntityRegistry,
    table: ExtractedTable,
    rows: list[ExtractedTableRow],
    headers: list[str],
) -> None:
    """Extract generic register/control entities from register-style tables."""

    register_name = _extract_register_name(table.section_title) or table.section_title or table.table_title
    if not register_name:
        register_name = f"Register page {table.page_number}"
    register_key = registry.add_entity(
        entity_family="register_or_control_item",
        display_text=register_name,
        aliases=_extract_identifier_aliases(register_name),
        confidence=0.9,
        metadata={"source": "register_table"},
    )
    registry.add_evidence(
        entity_key=register_key,
        page_number=table.page_number,
        source_kind="table",
        table_index=table.table_index,
        evidence_text=f"{table.section_title or ''} {table.table_title or ''}".strip(),
        confidence=0.85,
    )

    field_index = _find_header_index(headers, {"field"})
    value_id_index = _find_header_index(headers, {"value id"})
    value_index = _find_header_index(headers, {"value"})
    description_index = _find_header_index(headers, {"description"})

    for row in rows:
        field_value = _cell(row, field_index)
        if not field_value:
            continue
        field_key = registry.add_entity(
            entity_family="register_or_control_item",
            display_text=field_value,
            aliases=_extract_identifier_aliases(field_value),
            confidence=0.85,
            metadata={"source": "register_field"},
        )
        registry.add_evidence(
            entity_key=field_key,
            page_number=row.page_number,
            source_kind="table_row",
            table_index=row.table_index,
            row_index=row.row_index,
            evidence_text=row.text_rendering,
            confidence=0.85,
        )
        registry.add_relation(
            source_entity_key=field_key,
            relation_type="described_in",
            target_entity_key=register_key,
            confidence=0.85,
            metadata={"page_number": row.page_number},
        )

        description = _cell(row, description_index)
        if description:
            feature_key = registry.add_entity(
                entity_family="interface_or_feature",
                display_text=description,
                aliases=_extract_identifier_aliases(description),
                confidence=0.65,
                metadata={"source": "register_description"},
            )
            registry.add_evidence(
                entity_key=feature_key,
                page_number=row.page_number,
                source_kind="table_row",
                table_index=row.table_index,
                row_index=row.row_index,
                evidence_text=row.text_rendering,
                confidence=0.6,
            )
            registry.add_relation(
                source_entity_key=field_key,
                relation_type="configured_by",
                target_entity_key=feature_key,
                confidence=0.6,
                metadata={"page_number": row.page_number},
            )

        value_tokens = [token for token in [_cell(row, value_id_index), _cell(row, value_index)] if token]
        if value_tokens:
            spec_text = " / ".join(value_tokens)
            spec_key = registry.add_entity(
                entity_family="spec_item",
                display_text=spec_text,
                aliases=value_tokens,
                confidence=0.68,
                metadata={"source": "register_value"},
            )
            registry.add_evidence(
                entity_key=spec_key,
                page_number=row.page_number,
                source_kind="table_row",
                table_index=row.table_index,
                row_index=row.row_index,
                evidence_text=row.text_rendering,
                confidence=0.68,
            )
            registry.add_relation(
                source_entity_key=field_key,
                relation_type="has_spec",
                target_entity_key=spec_key,
                confidence=0.68,
                metadata={"page_number": row.page_number},
            )


def _extract_spec_entities(
    *,
    registry: _EntityRegistry,
    component_keys: list[str],
    table: ExtractedTable,
    rows: list[ExtractedTableRow],
    headers: list[str],
) -> None:
    """Extract generic electrical/timing spec rows."""

    parameter_index = _find_header_index(headers, {"parameter", "item", "spec", "rating"})
    if parameter_index is None:
        parameter_index = 0

    for row in rows:
        if row.row_type == "group_header":
            continue
        parameter = _cell(row, parameter_index)
        if not parameter:
            continue
        rendered = []
        for header, cell in zip(headers, row.cells):
            if not cell:
                continue
            rendered.append(f"{header}: {cell}" if header else cell)
        spec_text = " | ".join(rendered) or parameter
        spec_key = registry.add_entity(
            entity_family="spec_item",
            display_text=parameter,
            raw_text=spec_text,
            aliases=[parameter, spec_text],
            confidence=0.82,
            metadata={"source": "spec_table"},
        )
        registry.add_evidence(
            entity_key=spec_key,
            page_number=row.page_number,
            source_kind="table_row",
            table_index=row.table_index,
            row_index=row.row_index,
            evidence_text=row.text_rendering,
            confidence=0.82,
        )
        for component_key in component_keys:
            registry.add_relation(
                source_entity_key=component_key,
                relation_type="has_spec",
                target_entity_key=spec_key,
                confidence=0.78,
                metadata={"page_number": row.page_number},
            )


def _extract_generic_feature_entities(
    *,
    registry: _EntityRegistry,
    component_keys: list[str],
    table: ExtractedTable,
    rows: list[ExtractedTableRow],
    headers: list[str],
) -> None:
    """Extract feature-like entities from unclassified but structured rows."""

    del headers
    for row in rows:
        text = row.text_rendering
        for feature_text in _extract_feature_texts(text):
            feature_key = registry.add_entity(
                entity_family="interface_or_feature",
                display_text=feature_text,
                aliases=_extract_identifier_aliases(feature_text),
                confidence=0.58,
                metadata={"source": "generic_table"},
            )
            registry.add_evidence(
                entity_key=feature_key,
                page_number=row.page_number,
                source_kind="table_row",
                table_index=row.table_index,
                row_index=row.row_index,
                evidence_text=row.text_rendering,
                confidence=0.58,
            )
            for component_key in component_keys:
                registry.add_relation(
                    source_entity_key=component_key,
                    relation_type="supports_feature",
                    target_entity_key=feature_key,
                    confidence=0.55,
                    metadata={"page_number": row.page_number},
                )


def _apply_mcu_enricher(registry: _EntityRegistry) -> None:
    """Add stronger alias coverage for MCU-style terminals and registers."""

    for entity in registry.entities.values():
        if entity.entity_family not in {"signal_or_terminal", "register_or_control_item"}:
            continue
        extra_aliases = []
        for alias in entity.aliases:
            extra_aliases.extend(_extract_identifier_aliases(alias))
            if "_" in alias:
                extra_aliases.append(alias.replace("_", " "))
            if "/" in alias:
                extra_aliases.append(alias.replace("/", " "))
        entity.aliases = _unique_strings([*entity.aliases, *extra_aliases])


def _classify_table_family(*, headers: list[str], table_kind: str) -> str:
    """Map a structured table into one generic family."""

    header_set = set(headers)
    if table_kind == "register_table" or REGISTER_HEADERS.issubset(header_set):
        return "register_table"
    if (header_set & TERMINAL_HEADERS) and (header_set & NAME_HEADERS):
        return "terminal_table"
    if header_set & VARIANT_HEADERS:
        return "variant_table"
    if header_set & SPEC_HEADERS and len(header_set & {"min", "typ", "max", "unit"}) >= 2:
        return "spec_table"
    return "generic_table"


def _normalize_headers(headers: list[str | None]) -> list[str]:
    """Normalize row headers for table-family classification."""

    normalized = []
    for header in headers:
        text = _clean_text(header or "").lower()
        normalized.append(text)
    return normalized


def _find_header_index(headers: list[str], candidates: set[str]) -> int | None:
    """Return the first matching header index from a normalized header list."""

    for index, header in enumerate(headers):
        if header in candidates:
            return index
    return None


def _cell(row: ExtractedTableRow, index: int | None) -> str | None:
    """Safely fetch a cell by index."""

    if index is None or index >= len(row.cells):
        return None
    value = row.cells[index]
    return _clean_text(value) if value else None


def _extract_register_name(section_title: str | None) -> str | None:
    """Pull the register/control identifier from a numbered heading."""

    if not section_title:
        return None
    match = re.match(r"^\d+(?:\.\d+)+\s+(.+)$", section_title)
    if match:
        return _clean_text(match.group(1))
    return _clean_text(section_title)


def _extract_identifier_aliases(text: str) -> list[str]:
    """Capture exact technical identifiers and lightweight alias variants."""

    cleaned = _clean_text(text)
    if not cleaned:
        return []
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.\[\]/+\-]*", cleaned)
    aliases: list[str] = []
    for token in tokens:
        aliases.append(token)
        stripped = token.strip(".,;:()")
        if stripped != token:
            aliases.append(stripped)
        if any(character in token for character in "./[]_-+"):
            aliases.append(re.sub(r"[\[\]/_.+\-]+", " ", stripped).strip())
    return _unique_strings(aliases)


def _extract_feature_texts(*texts: str | None) -> list[str]:
    """Extract generic interface/feature phrases from row content."""

    features: list[str] = []
    for text in texts:
        cleaned = _clean_text(text or "")
        if not cleaned:
            continue
        lowered = cleaned.lower()
        for keyword in FEATURE_KEYWORDS:
            if keyword in lowered:
                features.append(keyword.upper() if len(keyword) <= 5 else keyword.title())
        for chunk in re.split(r"\s*/\s*|\s*\|\s*|;\s*", cleaned):
            chunk = _clean_text(chunk)
            if 2 <= len(chunk) <= 48 and any(char.isalpha() for char in chunk):
                if re.search(r"[A-Z]{2,}\d*", chunk) or any(term in chunk.lower() for term in FEATURE_KEYWORDS):
                    features.append(chunk)
    return _unique_strings(features)


def _first_relevant_rank(results: list[object], case: dict[str, object]) -> int | None:
    """Find the first rank satisfying the compact eval expectations."""

    expected_pages = case.get("expected_page")
    if expected_pages is None:
        expected_page_values: set[int] | None = None
    elif isinstance(expected_pages, list):
        expected_page_values = {int(value) for value in expected_pages}
    else:
        expected_page_values = {int(expected_pages)}

    expected_entity = _clean_text(str(case.get("expected_entity", ""))).lower()
    for rank, result in enumerate(results, start=1):
        page_ok = expected_page_values is None or result.page_number in expected_page_values
        entity_text = _clean_text(getattr(result, "entity_display_text", "")).lower()
        text_ok = not expected_entity or expected_entity in entity_text or expected_entity in result.source_text.lower()
        if page_ok and text_ok:
            return rank
    return None


def _family_match(results: list[object], case: dict[str, object]) -> bool | None:
    """Check whether the expected family is surfaced first."""

    expected_family = case.get("expected_top_result_family")
    if expected_family is None or not results:
        return None
    return getattr(results[0], "entity_family", None) == expected_family


def _evidence_match(query_response: QueryResponse, case: dict[str, object]) -> bool | None:
    """Check whether the query evidence pack includes the expected fact."""

    expected = case.get("expected_evidence_substring")
    if expected is None:
        return None
    expected_values = expected if isinstance(expected, list) else [expected]
    corpus_parts = [query_response.retrieval_summary, *query_response.coverage_notes]
    for group in query_response.evidence_groups:
        corpus_parts.append(str(group.get("summary", "")))
        corpus_parts.append(str(group.get("section_title", "")))
        corpus_parts.append(str(group.get("table_title", "")))
        for item in group.get("items", []):
            corpus_parts.append(str(item.get("text", "")))
            corpus_parts.append(str(item.get("entity_display_text", "")))
            headers = item.get("headers")
            cells = item.get("cells")
            if headers is not None:
                corpus_parts.append(" ".join(str(header) for header in headers if header))
            if cells is not None:
                corpus_parts.append(" ".join(str(cell) for cell in cells if cell))
    corpus = " ".join(part for part in corpus_parts if part).lower()
    return all(str(value).lower() in corpus for value in expected_values)


def _planner_trace_match(query_response: QueryResponse, case: dict[str, object]) -> bool | None:
    """Check whether the query planner trace includes expected expansion terms."""

    expected = case.get("expected_planner_terms")
    if expected is None:
        return None
    expected_values = expected if isinstance(expected, list) else [expected]
    trace_parts = [
        query_response.planner_mode,
        query_response.rerank_mode,
        query_response.intent,
        query_response.primary_subject,
        *query_response.must_include_terms,
        *query_response.should_include_terms,
        *query_response.identifier_terms,
        *query_response.table_family_preferences,
        *query_response.preferred_evidence_families,
        *query_response.subquestions,
        *query_response.section_hints,
        *query_response.negative_terms,
        query_response.retrieval_summary,
    ]
    trace = " ".join(part for part in trace_parts if part).lower()
    return all(str(value).lower() in trace for value in expected_values)


def _clean_text(text: str) -> str:
    """Normalize spacing for stable text comparisons."""

    return re.sub(r"\s+", " ", text).strip()


def _unique_strings(values: list[str]) -> list[str]:
    """Deduplicate strings while preserving order."""

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique
