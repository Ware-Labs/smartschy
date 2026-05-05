# Precision PCB QA Pipeline

This repository contains a local, file-based pipeline for answering PCB design
questions using:

- deterministic connectivity from Altium Specctra DSN,
- BOM identity mapping,
- section-aware PDF chunking for schematic/datasheets,
- an LLM-driven multi-turn tool loop over deterministic local tools,
- citation-aware inference prompting.

## Quick start

1. Create a Python 3.11+ virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Build indices:
   - `python -m pcb_qa.cli ingest --project-root .`
4. Agent-driven ask (iterative evidence acquisition + trace):
   - `python -m pcb_qa.cli agent-ask --project-root . --question "is VDDIO connected correctly to the ICM-42605?"`
   - Shows live progress lines on stderr while running.
   - Supports `--quiet` to suppress progress output.
5. Agent-driven ask + final LLM answer:
   - `python -m pcb_qa.cli agent-ask --project-root . --question "is VDDIO connected correctly to the ICM-42605?" --answer-with-llm --model gpt-5 --image-detail high`
6. Optional: run local MCP evidence server:
   - `python -m pcb_qa.cli mcp-server`
7. Run validation harness:
   - `python -m pcb_qa.cli validate --project-root .`

Outputs are written to `derived/`.

Evidence-loop stop reasons are:
- `model_finalize`
- `max_tool_calls_reached`
- `max_iterations_reached`
- `llm_error`
