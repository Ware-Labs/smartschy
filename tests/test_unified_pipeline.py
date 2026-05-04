#!/usr/bin/env python3
"""Lightweight tests for unified project pipeline helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build_project_artifacts import _discover_bom
from datasheet_linker import extract_datasheet_identifiers, link_components_to_datasheets


class DiscoverBomTests(unittest.TestCase):
    def test_prefers_bom_name_matching_dsn_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            dsn_path = project_dir / "board_a.dsn"
            dsn_path.write_text("(pcb board_a)", encoding="utf-8")
            preferred = project_dir / "Bill of Materials-board_a.csv"
            preferred.write_text("h1,h2\n", encoding="utf-8")
            other = project_dir / "other.csv"
            other.write_text("x,y\n", encoding="utf-8")
            selected = _discover_bom(project_dir, dsn_path)
            self.assertEqual(selected, preferred)

    def test_raises_when_multiple_csv_and_no_preferred_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            dsn_path = project_dir / "board_a.dsn"
            dsn_path.write_text("(pcb board_a)", encoding="utf-8")
            (project_dir / "one.csv").write_text("x,y\n", encoding="utf-8")
            (project_dir / "two.csv").write_text("x,y\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _discover_bom(project_dir, dsn_path)


class DatasheetLinkerTests(unittest.TestCase):
    def test_identifier_extraction_reads_first_page_and_metadata(self) -> None:
        normalized_doc = {
            "metadata": {"title": "ICM-42605 Motion Sensor"},
            "pages": [{"text_blocks": [{"text": "Part Number ICM-42605 on cover"}]}],
        }
        extracted = extract_datasheet_identifiers(normalized_doc, Path("DS-000292-ICM-42605-v1.7_0.pdf"))
        self.assertIn("ICM42605", extracted["identifier_norms"])

    def test_linker_classifies_exact_ambiguous_and_unmatched(self) -> None:
        crosswalk = {
            "by_ref": {
                "U1": {"component_id": "U1", "bom": {"manufacturer": "Invensense", "part_number": "ICM-42605"}},
                "U2": {"component_id": "U2", "bom": {"manufacturer": "Vendor", "part_number": "ABC-123"}},
                "U3": {"component_id": "U3", "bom": {"manufacturer": "Vendor", "part_number": "UNKNOWN-9"}},
            }
        }
        datasheets = [
            {
                "datasheet_id": "icm-sheet",
                "datasheet_markdown": "a.md",
                "identifier_norms": ["ICM42605"],
                "search_blob_norm": "ICM42605",
            },
            {
                "datasheet_id": "abc-one",
                "datasheet_markdown": "b.md",
                "identifier_norms": ["ABC123"],
                "search_blob_norm": "ABC123",
            },
            {
                "datasheet_id": "abc-two",
                "datasheet_markdown": "c.md",
                "identifier_norms": ["ABC123"],
                "search_blob_norm": "ABC123",
            },
        ]
        linked = link_components_to_datasheets(crosswalk, datasheets)
        by_ref = {row["ref"]: row for row in linked["components"]}
        self.assertEqual(by_ref["U1"]["status"], "linked_exact")
        self.assertEqual(by_ref["U2"]["status"], "ambiguous")
        self.assertEqual(by_ref["U3"]["status"], "unmatched")


if __name__ == "__main__":
    unittest.main()
