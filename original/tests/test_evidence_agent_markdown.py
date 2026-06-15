from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcb_qa.evidence_agent import _write_timestamped_markdown_answer


class EvidenceAgentMarkdownTests(unittest.TestCase):
    def test_writes_timestamped_markdown_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            md_path = _write_timestamped_markdown_answer(
                out_dir=out_dir,
                question="What does U6 do?",
                answer_text="U6 is the charger IC.",
                model="gpt-5",
            )
            self.assertTrue(md_path.exists())
            self.assertEqual(md_path.suffix.lower(), ".md")
            self.assertIn("responses", str(md_path))
            body = md_path.read_text(encoding="utf-8")
            self.assertIn("What does U6 do?", body)
            self.assertIn("U6 is the charger IC.", body)


if __name__ == "__main__":
    unittest.main()

