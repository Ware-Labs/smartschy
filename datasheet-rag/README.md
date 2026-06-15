# datasheet-rag

`datasheet-rag` is a lightweight local CLI for ingesting technical PDFs into an inspectable retrieval pipeline. Phase 3 now uses a visual-first table ingestion pipeline so pin/function rows can be stored and searched separately from prose chunks with better structural fidelity.

## Phase 3 scope

This phase provides:

- An installable Python package
- A Typer-based CLI
- Real `ingest` support using PyMuPDF
- Real `inspect` support backed by SQLite
- Real `search` support backed by SQLite FTS5 across prose chunks and table rows
- Stub commands for `query` and `eval`
- Shared configuration, logging, and storage helpers
- Page-aware prose chunking
- Visual-first table extraction and row-level storage
- A minimal pytest suite
- Inspectable debug artifacts per ingest

Entity extraction and richer retrieval ranking refinement begin in later phases.

## Local setup

1. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install the project in editable mode with test dependencies:

   ```powershell
   python -m pip install -e .[dev]
   ```

3. Confirm the CLI is wired up:

   ```powershell
   python -m datasheet_rag --help
   datasheet-rag --help
   ```

4. Run tests:

   ```powershell
   python -m pytest
   ```

## Phase 3 workflow

Ingest a PDF into SQLite and emit debug artifacts:

```powershell
datasheet-rag ingest path\to\file.pdf --db .\datasheets.db --out .\out
```

Force a full rebuild even when the same PDF hash is already present:

```powershell
datasheet-rag ingest path\to\file.pdf --db .\datasheets.db --out .\out --force
```

Inspect all stored documents:

```powershell
datasheet-rag inspect --db .\datasheets.db
```

Inspect one document with page previews:

```powershell
datasheet-rag inspect --db .\datasheets.db --doc <doc_id>
```

Search extracted prose chunks:

```powershell
datasheet-rag search --db .\datasheets.db "P1.04 UART"
```

When a match comes from a table row, search output also includes row metadata such as `row_type` and the stored crop artifact path.

## Intended workflow

Once later phases are implemented, the CLI will follow this shape:

```powershell
datasheet-rag ingest path\to\file.pdf --db .\datasheets.db --out .\out
datasheet-rag inspect --db .\datasheets.db --doc <doc_id>
datasheet-rag search --db .\datasheets.db "P1.04 UART"
datasheet-rag query --db .\datasheets.db "What functions are available on P1.04?"
datasheet-rag eval --db .\datasheets.db tests\nrf54l15_eval.yml
```

## Debug artifacts

Each ingest writes an artifact directory at `out/<doc_id>/` containing:

- `pages.jsonl`: one JSON object per page with page number and extracted text
- `chunks.jsonl`: one JSON object per chunk with page number, chunk index, and source text
- `tables.jsonl`: one JSON object per accepted table region with visual-parser metadata, crop path, native fallback text, and shape metadata
- `table_rows.jsonl`: one JSON object per canonical table row with `row_type`, structured visual cells, native fallback text, and search rendering
- `table_crops/`: rendered PNG crops for accepted tables only
- `document_summary.json`: top-level metadata for the ingest run

These files are intended for manual inspection while retrieval quality is still being tuned.

## Table ingestion design

Phase 3 keeps prose ingestion unchanged and applies a staged table pipeline:

1. Cheap PDF-native candidate discovery identifies likely table regions.
2. Accepted regions are cropped to images at a fixed DPI.
3. The crop parser defines canonical row and column structure.
4. Native PDF text inside the same region is used as reconciliation and fallback data.

This keeps ingestion local-only while making table structure more consistent on visually complex datasheet pages.

## Reingestion behavior

If the same PDF content hash is already stored and the expected debug artifacts already exist in the requested output directory, `ingest` now performs a true no-op and returns the existing counts immediately.

Use `--force` when you want to rebuild extraction artifacts after parser changes or debugging work.

## Project layout

```text
datasheet_rag/
  __init__.py
  __main__.py
  cli.py
  chunking.py
  config.py
  database.py
  logging_utils.py
  pdf_parser.py
  phase_stubs.py
  storage.py
  table_extraction.py
tests/
  test_cli.py
pyproject.toml
README.md
```

## Dependency philosophy

Phase 3 still keeps dependencies intentionally small:

- `PyMuPDF` for text extraction
- `Pillow` for crop rendering
- `numpy` for lightweight visual table segmentation
- `typer` for the CLI
- `pytest` for tests

SQLite comes from the Python standard library. Later phases will add entity extraction, scoring refinement, and optional embedding components only when needed.

## Next phase

Phase 4 will add datasheet-aware entity extraction and pin-aware ranking boosts.
