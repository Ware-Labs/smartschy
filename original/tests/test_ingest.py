from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from pcb_qa.ingest import ingest_project, ingest_project_with_inputs


class IngestBehaviorTests(unittest.TestCase):
    def test_ingest_project_with_inputs_requires_files(self) -> None:
        root = Path(".").resolve()
        with self.assertRaises(FileNotFoundError):
            ingest_project_with_inputs(
                project_root=root,
                dsn_path=root / "missing.dsn",
                bom_csv_path=root / "missing.csv",
                schematic_pdf=root / "missing.pdf",
                resources_dir=root / "missing_resources",
            )

    def test_ingest_project_calls_explicit_path_variant(self) -> None:
        root = Path(".").resolve()
        with mock.patch("pcb_qa.ingest.ingest_project_with_inputs", return_value={"ok": True}) as patched:
            result = ingest_project(project_root=root)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(patched.call_count, 1)


if __name__ == "__main__":
    unittest.main()

