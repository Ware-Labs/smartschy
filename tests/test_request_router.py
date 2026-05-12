from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcb_qa.request_router import route_request
from pcb_qa.utils import write_json


class RequestRouterTests(unittest.TestCase):
    def test_forced_precision_mode_bypasses_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "derived").mkdir(parents=True, exist_ok=True)
            write_json(root / "derived" / "ingest_summary.json", {"ok": True})
            decision = route_request(root, "explain this board", forced_mode="precision")
            self.assertEqual(decision.route, "precision")
            self.assertEqual(decision.confidence, "forced")

    def test_heuristic_irrelevant_route_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            decision = route_request(root, "what is the weather today in tokyo?")
            self.assertEqual(decision.route, "irrelevant_general")


if __name__ == "__main__":
    unittest.main()

