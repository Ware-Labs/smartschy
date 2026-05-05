# Precision PCB QA Pipeline

This repository contains a local, file-based pipeline for answering PCB design
questions using:

- deterministic connectivity from Altium Specctra DSN,
- BOM identity mapping,
- section-aware PDF chunking for schematic/datasheets,
- exact-first retrieval with constrained semantic expansion,
- citation-aware inference prompting.

## Quick start

1. Create a Python 3.11+ virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Build indices:
   - `python -m pcb_qa.cli ingest --project-root .`
4. Ask a question (builds evidence packet + prompt):
   - `python -m pcb_qa.cli ask --project-root . --question "did I connect the crystal correctly to the microcontroller?"`
5. Run validation harness:
   - `python -m pcb_qa.cli validate --project-root .`

Outputs are written to `derived/`.
