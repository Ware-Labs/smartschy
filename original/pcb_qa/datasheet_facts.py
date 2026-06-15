from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .model_policy import ANSWER_MODEL_DEFAULT
from .utils import read_json, write_json, write_jsonl

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


@dataclass
class DatasheetFactsOptions:
    llm_model: str = ANSWER_MODEL_DEFAULT
    overlap: float = 0.25
    early_stop: bool = True
    max_windows: int = 8


def _windows(text: str, overlap: float, window_size: int = 10000) -> list[str]:
    text = text.strip()
    if not text:
        return []
    overlap = min(max(overlap, 0.0), 0.9)
    step = max(1, int(window_size * (1.0 - overlap)))
    out: list[str] = []
    cursor = 0
    while cursor < len(text):
        out.append(text[cursor : cursor + window_size])
        cursor += step
    return out


def _extract_tables(md_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in md_text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        if len(cells) < 2:
            continue
        lowered = [cell.lower() for cell in cells]
        if any("pin" in cell for cell in lowered):
            continue
        pin_name = cells[0]
        if not re.search(r"[A-Za-z0-9]", pin_name):
            continue
        desc = cells[1] if len(cells) > 1 else ""
        fn = cells[2] if len(cells) > 2 else desc
        rows.append(
            {
                "pin_name": pin_name,
                "description": desc,
                "function": fn,
                "raw_row": " | ".join(cells),
            }
        )
    return rows


def _first_description(md_text: str) -> str:
    for line in md_text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or cleaned.startswith("|"):
            continue
        if len(cleaned) < 20:
            continue
        return cleaned[:400]
    return ""


def _llm_extract(window: str, model: str, api_key: str) -> dict[str, Any]:
    if OpenAI is None:
        return {}
    prompt = {
        "task": "Extract component facts from datasheet markdown chunk.",
        "constraints": [
            "Return JSON object only.",
            "Keep uncertainty explicit.",
            "Pin table rows must include pin_name, description, function, raw_row.",
        ],
        "required_fields": ["part_description", "pin_table_rows"],
        "text_chunk": window[:18000],
    }
    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=model, input=json.dumps(prompt, ensure_ascii=True))
        raw = response.output_text.strip()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return {}
        if not isinstance(payload.get("pin_table_rows"), list):
            payload["pin_table_rows"] = []
        return payload
    except Exception:
        return {}


def extract_component_facts(
    project_root: Path | str,
    options: DatasheetFactsOptions | None = None,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    options = options or DatasheetFactsOptions()
    bom_map = read_json(project_root / "derived" / "bom" / "refdes_to_part.json")
    md_manifest_path = project_root / "derived" / "datasheets" / "datasheet_markdown_manifest.json"
    manifest = read_json(md_manifest_path) if md_manifest_path.exists() else {"items": []}
    by_pdf = {str(item.get("pdf_name", "")): str(item.get("markdown_path", "")) for item in manifest.get("items", [])}

    out_dir = project_root / "derived" / "datasheets"
    out_dir.mkdir(parents=True, exist_ok=True)
    component_dir = out_dir / "components"
    component_dir.mkdir(parents=True, exist_ok=True)

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    fact_rows: list[dict[str, Any]] = []
    pin_rows: list[dict[str, Any]] = []
    for refdes, meta in bom_map.items():
        candidates = meta.get("datasheet_candidates", [])
        md_path = ""
        for candidate in candidates:
            if candidate in by_pdf:
                md_path = by_pdf[candidate]
                break
        part_desc = ""
        normalized_rows: list[dict[str, str]] = []
        if md_path and Path(md_path).exists():
            md_text = Path(md_path).read_text(encoding="utf-8", errors="replace")
            part_desc = _first_description(md_text)
            normalized_rows = _extract_tables(md_text)
            for window_idx, window in enumerate(_windows(md_text, overlap=options.overlap)):
                if window_idx >= options.max_windows:
                    break
                if api_key and (not part_desc or not normalized_rows):
                    llm_payload = _llm_extract(window, model=options.llm_model, api_key=api_key)
                    if not part_desc:
                        part_desc = str(llm_payload.get("part_description", "")).strip() or part_desc
                    if not normalized_rows:
                        llm_rows = llm_payload.get("pin_table_rows", [])
                        if isinstance(llm_rows, list):
                            normalized_rows = [row for row in llm_rows if isinstance(row, dict)]
                if options.early_stop and part_desc and normalized_rows:
                    break

        fact = {
            "refdes": refdes,
            "part_number": meta.get("part_number", ""),
            "manufacturer": meta.get("manufacturer", ""),
            "datasheet_markdown_path": md_path,
            "part_description": part_desc,
            "has_pin_table": bool(normalized_rows),
        }
        fact_rows.append(fact)
        for row in normalized_rows:
            pin_rows.append(
                {
                    "refdes": refdes,
                    "part_number": meta.get("part_number", ""),
                    "pin_name": str(row.get("pin_name", "")),
                    "description": str(row.get("description", "")),
                    "function": str(row.get("function", "")),
                    "raw_row": str(row.get("raw_row", "")),
                }
            )
        md_lines = [
            f"# Component Facts: {refdes}",
            "",
            f"- Part number: {meta.get('part_number', '')}",
            f"- Manufacturer: {meta.get('manufacturer', '')}",
            "",
            "## Part Description",
            "",
            part_desc or "_Not extracted_",
            "",
            "## Normalized Pin Table",
            "",
        ]
        if normalized_rows:
            md_lines.append("| Pin | Description | Function |")
            md_lines.append("| --- | --- | --- |")
            for row in normalized_rows[:200]:
                md_lines.append(
                    f"| {row.get('pin_name', '')} | {row.get('description', '')} | {row.get('function', '')} |"
                )
        else:
            md_lines.append("_No pin table extracted_")
        (component_dir / f"{refdes}.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    write_jsonl(out_dir / "component_facts.jsonl", fact_rows)
    write_jsonl(out_dir / "component_pin_functions.jsonl", pin_rows)
    manifest_payload = {
        "component_count": len(fact_rows),
        "pin_function_rows": len(pin_rows),
        "component_dir": str(component_dir),
        "component_facts_path": str(out_dir / "component_facts.jsonl"),
        "component_pin_functions_path": str(out_dir / "component_pin_functions.jsonl"),
        "llm_model": options.llm_model,
        "overlap": options.overlap,
        "early_stop": options.early_stop,
    }
    write_json(out_dir / "datasheet_facts_manifest.json", manifest_payload)
    return manifest_payload

