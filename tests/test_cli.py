from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from pcb_qa.cli import _AgentAskProgressReporter


REPO_ROOT = Path(__file__).resolve().parents[1]


def _has_required_artifacts() -> bool:
    required = [
        REPO_ROOT / "derived" / "dsn" / "pin_to_net.json",
        REPO_ROOT / "derived" / "pdf" / "pdf_chunks.jsonl",
        REPO_ROOT / "derived" / "pdf" / "schematic_page_images.json",
    ]
    return all(path.exists() for path in required)


class CliBehaviorTests(unittest.TestCase):
    def test_legacy_ask_command_removed(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcb_qa.cli",
                "ask",
                "--project-root",
                ".",
                "--question",
                "is VDDIO connected correctly to the ICM-42605?",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        stderr = proc.stderr.lower()
        self.assertIn("invalid choice", stderr)
        self.assertIn("agent-ask", stderr)

    @unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
    def test_agent_ask_shows_progress_on_stderr_and_json_on_stdout(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcb_qa.cli",
                "agent-ask",
                "--project-root",
                ".",
                "--question",
                "is VDDIO connected correctly to the ICM-42605?",
                "--max-iterations",
                "2",
                "--max-tool-calls",
                "24",
                "--max-chunks",
                "8",
                "--max-schematic-images",
                "2",
                "--max-total-evidence-items",
                "24",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("[agent-ask]", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("question", payload)
        self.assertIn("stop_reason", payload)

    @unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
    def test_agent_ask_quiet_suppresses_stderr_progress(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcb_qa.cli",
                "agent-ask",
                "--project-root",
                ".",
                "--question",
                "is VDDIO connected correctly to the ICM-42605?",
                "--max-iterations",
                "2",
                "--max-tool-calls",
                "24",
                "--max-chunks",
                "8",
                "--max-schematic-images",
                "2",
                "--max-total-evidence-items",
                "24",
                "--quiet",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")
        payload = json.loads(proc.stdout)
        self.assertIn("question", payload)

    def test_progress_reporter_starts_and_stops_spinner_for_llm_wait(self) -> None:
        reporter = _AgentAskProgressReporter(quiet=False)
        with (
            mock.patch.object(reporter, "_start_spinner") as start_spinner,
            mock.patch.object(reporter, "_stop_spinner") as stop_spinner,
            mock.patch.object(reporter, "_print_line") as print_line,
        ):
            reporter.callback("Submitting LLM request (attached images: 2)")
            reporter.callback("Wrote final answer: derived/qa/agent_answer.txt")

        start_spinner.assert_called_once()
        self.assertGreaterEqual(stop_spinner.call_count, 1)
        self.assertEqual(print_line.call_count, 2)


if __name__ == "__main__":
    unittest.main()
