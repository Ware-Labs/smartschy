# datasheet-rag

`datasheet-rag` is a local CLI for ingesting electronics datasheets into an inspectable SQLite-backed retrieval system.

It currently includes:

- PDF text extraction
- hybrid text-first table extraction
- auto-parallel ingest with optional worker override
- structured table-row retrieval
- generic datasheet entity extraction
- entity-aware ranking
- evidence-pack `evidence` for external LLM use
- reserved `answer` stage for future final-answer generation
- compact local `eval`

The design goal is not opaque RAG magic. It is a repeatable, debuggable pipeline where the intermediate artifacts stay visible and inspectable.

## Current scope

The project currently supports:

- `ingest`: parse a PDF, store canonical records in SQLite, and emit debug artifacts
- `inspect`: inspect stored document metadata and extracted page text
- `search`: run lexical plus entity-aware retrieval over prose chunks and table rows
- `evidence`: assemble a structured evidence pack with page/row provenance
- `answer`: reserved entrypoint for the future final-answer stage
- `eval`: run a compact JSON evaluation harness over retrieval and evidence usefulness

The implementation is generic-first for electronics datasheets. It does not assume every document is an MCU datasheet, though MCU-style register and pin tables are first-class citizens.

## Architecture

The pipeline is layered:

1. PDF ingest
2. prose chunking
3. table extraction
4. entity extraction
5. entity-aware retrieval
6. evidence-pack assembly

### Prose ingest

Each page is extracted with PyMuPDF and stored in:

- `pages`
- `chunks`

Chunking is page-aware and optimized for local lexical retrieval.

### Table ingest

Table ingestion is hybrid text-first, not pure visual parsing.

Canonical behavior:

- register / control tables prefer native PDF text plus word geometry
- pin / terminal tables prefer native PDF text plus word geometry
- display-heavy summary tables can fall back to visual parsing
- accepted tables are stored as logical tables and row-level retrieval records

This lets the system recover structured technical rows such as:

- `P1.04`
- `AIN0`
- `SUBSCRIBE_XOSTOP`
- `CHIDX`
- `Disabled / Enabled`

without depending only on rendered image parsing.

### Parallel ingest

Large ingests can use multiple worker processes.

Default behavior:

- worker mode is automatic
- logical CPU count is detected
- page count is considered
- a tiny startup probe can be used to choose a better worker count
- SQLite writes remain centralized in the parent process

You can still force a specific worker count with `--workers N`.

### Entity layer

Phase 4 adds a generic datasheet entity layer built from the already-ingested artifacts.

Core entity families:

- `component`
- `interface_or_feature`
- `signal_or_terminal`
- `register_or_control_item`
- `spec_item`
- `table_object`
- `section_object`

Core relation families currently include:

- `has_variant`
- `has_terminal`
- `supports_feature`
- `maps_to_function`
- `configured_by`
- `described_in`
- `has_spec`

Extraction is driven by common datasheet structures rather than a single device family:

- terminal / pin / ball / pad tables
- variant / package / ordering tables
- register / control tables
- electrical / timing / operating spec tables
- generic structured feature tables

There is also a modest MCU-style enricher for stronger alias coverage on terminals and control items.

## Storage model

SQLite is the canonical store.

Main tables:

- `documents`
- `pages`
- `chunks`
- `tables`
- `table_rows`
- `entities`
- `entity_evidence`
- `entity_relations`

FTS5 is used for:

- prose chunk text
- table-row text renderings

Search ranking then combines lexical matches with entity-aware boosts.

## Debug artifacts

Each ingest writes an artifact directory:

```text
out/<document_id>/
```

Typical files:

- `pages.jsonl`
- `chunks.jsonl`
- `tables.jsonl`
- `table_rows.jsonl`
- `entities.jsonl`
- `entity_evidence.jsonl`
- `entity_relations.jsonl`
- `document_summary.json`
- `table_crops/`

These artifacts are intended to be human-inspectable.

## Local setup

1. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install the package with dev dependencies:

```powershell
python -m pip install -e .[dev]
```

3. Check the CLI:

```powershell
python -m datasheet_rag --help
datasheet-rag --help
```

4. Run tests:

```powershell
python -m pytest
```

5. Optional: configure the built-in OpenAI evidence planner:

```powershell
$env:OPENAI_API_KEY = "your_api_key"
$env:DATASHEET_RAG_QUERY_MODEL = "gpt-4.1-mini"
```

If these variables are unset, `evidence` still works by using the deterministic local fallback planner.

## CLI usage

### Ingest

Ingest a PDF into SQLite and emit debug artifacts:

```powershell
datasheet-rag ingest datasheets\example.pdf --db .\datasheets.db --out .\out
```

Force a rebuild:

```powershell
datasheet-rag ingest datasheets\example.pdf --db .\datasheets.db --out .\out --force
```

Force serial ingest:

```powershell
datasheet-rag ingest datasheets\example.pdf --db .\datasheets.db --out .\out --workers 1
```

Force a specific worker count:

```powershell
datasheet-rag ingest datasheets\example.pdf --db .\datasheets.db --out .\out --workers 8
```

Current ingest output includes:

- `document_id`
- `page_count`
- `chunk_count`
- `table_count`
- `table_row_count`
- `table_candidate_count`
- `entity_count`
- `entity_relation_count`
- `worker_mode`
- `selected_worker_count`
- `probe_ran`
- `artifacts_dir`

### Inspect

Inspect all stored documents:

```powershell
datasheet-rag inspect --db .\datasheets.db
```

Inspect one document with page previews and metadata:

```powershell
datasheet-rag inspect --db .\datasheets.db --doc <document_id>
```

### Search

Search across prose chunks and structured table rows:

```powershell
datasheet-rag search --db .\datasheets.db "P1.04"
datasheet-rag search --db .\datasheets.db "Pin configuration"
datasheet-rag search --db .\datasheets.db "XR1234B package"
```

Search output can include:

- page number
- chunk type
- table and row indexes
- row type
- section title
- table title
- crop path
- structured headers and cells
- entity family and matched entity display text
- lexical and final ranking scores

### Evidence

Assemble a structured local evidence pack:

```powershell
datasheet-rag evidence --db .\datasheets.db "What function does P1.04 provide?"
datasheet-rag evidence --db .\datasheets.db "What are the canonical pins for QSPI?"
datasheet-rag evidence --db .\datasheets.db "Which package does XR1234B use?"
```

Current `evidence` behavior is intentionally simple:

- optionally plan the question with the built-in OpenAI planner before retrieval
- run an evidence-specific seed retrieval pass
- expand around strong seeds with deterministic grep-style scans
- prefer coherent structured table evidence over isolated prose snippets
- print grouped evidence blocks optimized for copy/paste into an external LLM
- show a planner trace, including preferred table families and expansion terms
- avoid local answer synthesis by default

When configured, the OpenAI planner is used only for question expansion and retrieval planning. It does not answer the question. If planning fails or the environment is not configured, `evidence` reports that it used the deterministic local fallback instead.

`query` remains available as a hidden compatibility alias for `evidence`.

### Answer

Reserve the final answer stage:

```powershell
datasheet-rag answer --db .\datasheets.db "What are the canonical pins for QSPI?"
```

Current `answer` behavior is intentionally conservative:

- it does not synthesize a final answer yet
- it emits a clear notice that final answer generation is still reserved
- it then prints the same interim evidence pack that would feed a downstream LLM

### Eval

Run the compact evaluation harness:

```powershell
datasheet-rag eval .\tests\eval.json --db .\datasheets.db
```

Current eval input is JSON, not YAML.

A minimal example:

```json
{
  "cases": [
    {
      "query": "XR1234B package",
      "expected_page": 1,
      "expected_entity": "XR1234B",
      "expected_top_result_family": "component",
      "expected_evidence_substring": ["XR1234B", "DFN-6"],
      "expected_planner_terms": ["package", "ordering information"]
    }
  ]
}
```

Current eval output reports metrics such as:

- `hit_at_1`
- `hit_at_3`
- `hit_at_5`
- `mean_first_relevant_rank`
- `family_match_rate`
- `evidence_match_rate`
- `planner_trace_match_rate`

## Reingestion behavior

If the same PDF hash is already stored and the expected artifacts exist, ingest performs a true no-op skip.

That skip is only taken when the stored parser metadata matches the current code expectations, including:

- table parser version
- entity parser version

Use `--force` when you want to rebuild after parser changes or debugging work.

## Example workflow

```powershell
datasheet-rag ingest datasheets\nRF54L15_nRF54L10_nRF54L05_Datasheet_v1.0.pdf --db .\datasheets.db --out .\out
datasheet-rag inspect --db .\datasheets.db --doc <document_id>
datasheet-rag search --db .\datasheets.db "P1.04"
datasheet-rag search --db .\datasheets.db "Pin configuration"
datasheet-rag evidence --db .\datasheets.db "What are the canonical pins for QSPI?"
datasheet-rag eval .\tests\eval.json --db .\datasheets.db
```

## Project layout

```text
datasheet_rag/
  __init__.py
  __main__.py
  cli.py
  chunking.py
  config.py
  database.py
  entities.py
  logging_utils.py
  parallel_ingest.py
  pdf_parser.py
  phase_stubs.py
  storage.py
  table_extraction.py
tests/
  conftest.py
  test_cli.py
  test_parallel_ingest.py
pyproject.toml
README.md
```

## Dependencies

Runtime dependencies are intentionally small:

- `pymupdf`
- `pillow`
- `numpy`
- `typer`

Dev/test dependency:

- `pytest`

SQLite is provided by the Python standard library.

## Current limitations

- retrieval is still lexical-plus-entity-aware, not embedding-based
- `evidence` is deliberately evidence-first and does not try to answer locally
- `answer` is only a reserved entrypoint today; final answer generation is not implemented yet
- eval is compact and local, not a large benchmark framework
- table parsing is much stronger than earlier phases, but complex PDFs can still contain hard edge cases

## Status

Implemented through Phase 4:

- real ingest
- inspectable artifact output
- hybrid text-first table parsing
- auto worker selection for parallel ingest
- generic datasheet entity extraction
- entity-aware ranking
- evidence-pack `evidence`
- reserved `answer` entrypoint
- compact `eval`
