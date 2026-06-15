from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcb_qa.general_response import run_general_response
from pcb_qa.request_router import RouteDecision
from pcb_qa.utils import write_json


class GeneralResponseTests(unittest.TestCase):
    def test_general_response_fallback_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "derived" / "pdf").mkdir(parents=True, exist_ok=True)
            write_json(root / "derived" / "pdf" / "schematic_page_images.json", {"images": []})
            (root / "derived" / "kg").mkdir(parents=True, exist_ok=True)
            (root / "derived" / "kg" / "circuit_summary.md").write_text("# summary\n", encoding="utf-8")
            decision = RouteDecision(
                route="relevant_general",
                confidence="high",
                rationale="test",
                model_used="heuristic",
            )
            payload = run_general_response(root, "What does this board do?", decision, model="gpt-5")
            self.assertEqual(payload["mode"], "general")
            self.assertIn("answer_text", payload)


if __name__ == "__main__":
    unittest.main()

