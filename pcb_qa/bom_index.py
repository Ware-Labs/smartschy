from __future__ import annotations

import csv
from pathlib import Path

from .utils import write_json, write_jsonl


def _split_designators(raw_designators: str) -> list[str]:
    if not raw_designators:
        return []
    return [item.strip() for item in raw_designators.split(",") if item.strip()]


def build_bom_indices(bom_csv_path: Path, resources_dir: Path, output_dir: Path) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    component_rows: list[dict] = []
    refdes_to_part: dict[str, dict] = {}

    with bom_csv_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not row.get("Designator"):
                continue
            designators = _split_designators(row.get("Designator", ""))
            part_record = {
                "designators": designators,
                "quantity": row.get("Quantity", "").strip(),
                "value": row.get("Value", "").strip(),
                "manufacturer": row.get("Manufacturer", "").strip(),
                "part_number": row.get("Part Number", "").strip(),
                "note": row.get("Note", "").strip(),
                "specification": row.get("Specification", "").strip(),
                "footprint": row.get("Footprint", "").strip(),
            }
            component_rows.append(part_record)
            for refdes in designators:
                refdes_to_part[refdes] = {
                    "part_number": part_record["part_number"],
                    "manufacturer": part_record["manufacturer"],
                    "value": part_record["value"],
                    "footprint": part_record["footprint"],
                    "specification": part_record["specification"],
                }

    resources_lookup = {
        pdf.stem.lower(): str(pdf.name) for pdf in sorted(resources_dir.glob("*.pdf"))
    }
    for refdes, payload in refdes_to_part.items():
        mpn = payload.get("part_number", "").lower().replace("/", "_")
        candidates = []
        for stem, filename in resources_lookup.items():
            if mpn and (mpn in stem or stem in mpn):
                candidates.append(filename)
        payload["datasheet_candidates"] = sorted(set(candidates))
        refdes_to_part[refdes] = payload

    write_jsonl(output_dir / "component_index.jsonl", component_rows)
    write_json(output_dir / "refdes_to_part.json", refdes_to_part)

    return {
        "component_rows": len(component_rows),
        "refdes_count": len(refdes_to_part),
    }

