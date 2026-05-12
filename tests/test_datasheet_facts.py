from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcb_qa.datasheet_facts import DatasheetFactsOptions, extract_component_facts
from pcb_qa.utils import write_json


class DatasheetFactsTests(unittest.TestCase):
    def test_extract_component_facts_from_markdown_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            datasheet_dir = root / "derived" / "datasheets" / "markdown"
            datasheet_dir.mkdir(parents=True, exist_ok=True)
            (root / "derived" / "bom").mkdir(parents=True, exist_ok=True)
            md_path = datasheet_dir / "part.md"
            md_path.write_text(
                "# Datasheet\n\nThis is a useful sensor IC.\n\n| Pin | Description | Function |\n| --- | --- | --- |\n| 1 | GND | Ground |\n| 2 | SDA | Data |\n",
                encoding="utf-8",
            )
            write_json(
                root / "derived" / "datasheets" / "datasheet_markdown_manifest.json",
                {
                    "items": [
                        {
                            "pdf_name": "part.pdf",
                            "markdown_path": str(md_path),
                        }
                    ]
                },
            )
            write_json(
                root / "derived" / "bom" / "refdes_to_part.json",
                {
                    "U1": {
                        "part_number": "PART-123",
                        "manufacturer": "Acme",
                        "datasheet_candidates": ["part.pdf"],
                    }
                },
            )
            payload = extract_component_facts(
                root,
                options=DatasheetFactsOptions(overlap=0.25, early_stop=True, max_windows=2),
            )
            self.assertEqual(payload["component_count"], 1)
            self.assertGreaterEqual(payload["pin_function_rows"], 1)
            self.assertTrue((root / "derived" / "datasheets" / "component_facts.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

