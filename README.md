# Generic DSN-Derived Review Artifacts

This workspace contains tools to parse Spectra DSN files and generate
deterministic, generic review artifacts that can be fed to an LLM for
connectivity/debug review workflows.

## Scripts

- `parse_dsn.py`
  - Parses DSN into one normalized internal schema JSON.
- `build_review_artifacts.py`
  - Builds a seven-file per-board artifact pack from a DSN file (BOM required).
- `review_artifacts.py`
  - Artifact-generation library used by the CLI.
- `bom_ingest.py`
  - BOM CSV ingestion and DSN/BOM join helpers.

## Build Artifacts

For each board, the pipeline emits:

1. `01_connectivity.core.json`
   - Canonical net/pin/reference graph and cross-indexes.
2. `02_components.catalog.json`
   - Placement catalog + library pin metadata + connected nets.
3. `03_routing.topology.json`
   - Routed wires/vias, layer usage, and per-net route summary.
4. `04_signal_views.json`
   - Generic per-net projections: fanout, neighbors, route metrics.
5. `05_integrity_checks.json`
   - Protocol-agnostic checks and findings.
6. `06_review_report.md`
   - Human-readable summary with pointers to the JSON artifacts.
7. `07_bom_crosswalk.json`
   - Canonical DSN/BOM cross-reference (matched, DSN-only, BOM-only, diagnostics).

## Usage

### 1) Normalize DSN

```bash
python parse_dsn.py keen3_filet/keen3_filet.dsn -o keen3_filet/keen3_filet.normalized.json
```

### 2) Build review artifacts (one command)

```bash
python build_review_artifacts.py --out-root derived --summary
```

This auto-discovers `.dsn` files and picks a BOM CSV in each DSN folder.
Preferred BOM filename pattern is:

- `Bill of Materials-<dsn-stem>.csv`

By default, each board goes to:

- `derived/<board-name>/01_connectivity.core.json`
- ...
- `derived/<board-name>/07_bom_crosswalk.json`

## Parser Order

1. Tokenize DSN text
2. Parse S-expression tree
3. Read root metadata
4. Parse structure
5. Parse placement
6. Parse library
7. Parse network
8. Parse wiring
9. Apply dedupe rules
10. Build cross-indexes
11. Emit normalized schema

## Dedupe Rules

- Stable dedupe for duplicate pins in a net (`pins_unique`)
- Stable dedupe for duplicate component references (first-seen wins)
- Exact tuple dedupe for duplicate wire segments
- Exact tuple dedupe for duplicate vias

## Integrity Checks (generic)

- Duplicate pin entries on a net
- Duplicate component references
- Orphan components (placed but not in nets)
- Unresolved pin references (net pin points to unknown reference)
- Nets without routing evidence
- Fragmented routed geometry (multi-island route graph)
- High-fanout review hints
- BOM reference missing in DSN
- DSN reference missing in BOM
- Footprint mismatch (DSN component identifier vs BOM footprint)
- BOM quantity mismatch vs exploded designator count
- Duplicate BOM reference rows
- Unparsed BOM designator tokens

## BOM Join Rules

- Parse the BOM with strict headers:
  - `Designator, Quantity, Value, Manufacturer, Part Number, Note, Specification, Footprint`
- Split BOM `Designator` into one normalized reference per token.
- Join BOM to DSN by normalized reference designator.
- Preserve per-row provenance (`row_id`, source line, raw text).
- Report unmatched references in `07_bom_crosswalk.json`.

## Determinism

JSON artifact outputs use stable key ordering and deterministic sorting to support
repeatable diffs and regression checks.

## First-Pass LLM Enricher

This repository includes a first-pass question-enrichment workflow that:

1. Builds a compact LLM-oriented board summary from derived artifacts.
2. Expands a user question into BOM/netlist-aware markdown.

### Scripts

- `build_llm_summary.py`
  - Input: board derived folder (`01/02/04/07` artifacts) + optional question.
  - Output: `llm_summary.json`.
- `enrich_question_first_pass.py`
  - Input: user question + `llm_summary.json`.
  - Output: `enriched_question.md`.
- `llm_openai_client.py`
  - OpenAI chat completions helper used by the first-pass script.
- `validate_enriched_question.py`
  - Validates whether enriched markdown is second-pass answerable.
- `tests/run_enriched_validation_tests.py`
  - Runs fixture-based validator tests (passing, endpoint-only, swapped, extra endpoint).

### Build summary

```bash
python build_llm_summary.py --board derived_onecmd/keen3_filet --question "are the swdio and swdclk lines connected properly?" --out llm/keen3_filet/llm_summary.json
```

### Enrich question (OpenAI)

Set API key:

```bash
set OPENAI_API_KEY=your_key_here
```

Run enrichment:

```bash
python enrich_question_first_pass.py --question "are the swdio and swdclk lines connected properly?" --summary llm/keen3_filet/llm_summary.json --out llm/keen3_filet/enriched_question.md --model gpt-4.1-mini
```

### Enrich question (offline/mock)

Use this when API key is unavailable:

```bash
python enrich_question_first_pass.py --question "are the swdio and swdclk lines connected properly?" --summary llm/keen3_filet/llm_summary.json --out llm/keen3_filet/enriched_question.md --mock
```

### Output contract

`enriched_question.md` always includes these sections:

- `## Restated Question`
- `## Net And Component Mapping`
- `## Candidate Signal Or Current Paths`
- `## Pin-Level Evidence`
- `## Expected Vs Actual Mapping`
- `## Connector Pinout Validation`
- `## Related Required Nets`
- `## Verification Status`
- `## Evidence Needed For Final Answer`
- `## Assumptions And Unknowns`
- `## Recommended Follow-Up Checks`

`## Verification Status` must include a JSON fenced block with:

- `verification_status` (`verified|likely_correct|inconclusive|failed`)
- `answerability_score` (0..1)
- `missing_evidence` (list)

### Validate enriched output

```bash
python validate_enriched_question.py --summary llm/keen3_filet/llm_summary.json --enriched llm/keen3_filet/enriched_question.md --out llm/keen3_filet/validation.json
```

### Run fixture regression tests

```bash
python tests/run_enriched_validation_tests.py
```
