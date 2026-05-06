from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pcb_qa import cli


class CliBehaviorTests(unittest.TestCase):
    def test_legacy_ask_command_removed(self) -> None:
        parser = cli._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["ask"])

    def test_agent_ask_shows_progress_on_stderr_and_json_on_stdout(self) -> None:
        fake_payload = {"question": "q", "stop_reason": "model_finalize"}
        fake_stdout = io.StringIO()
        fake_stderr = io.StringIO()

        def _fake_run_evidence_agent(**kwargs: object) -> dict[str, object]:
            callback = kwargs.get("progress_callback")
            if callable(callback):
                callback("Starting agent-ask for question: q")
                callback("Submitting LLM request (attached images: 0)")
                callback("Wrote final answer: derived/qa/agent_answer.txt")
            return fake_payload

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "pcb_qa.cli",
                    "agent-ask",
                    "--project-root",
                    ".",
                    "--question",
                    "q",
                ],
            ),
            mock.patch("pcb_qa.cli.run_evidence_agent", side_effect=_fake_run_evidence_agent),
            redirect_stdout(fake_stdout),
            redirect_stderr(fake_stderr),
        ):
            rc = cli.main()

        self.assertEqual(rc, 0)
        stderr = fake_stderr.getvalue()
        self.assertIn("[agent-ask]", stderr)
        payload = json.loads(fake_stdout.getvalue())
        self.assertIn("question", payload)
        self.assertIn("stop_reason", payload)

    def test_agent_ask_quiet_suppresses_stderr_progress(self) -> None:
        fake_payload = {"question": "q", "stop_reason": "model_finalize"}
        fake_stdout = io.StringIO()
        fake_stderr = io.StringIO()

        def _fake_run_evidence_agent(**kwargs: object) -> dict[str, object]:
            self.assertIsNone(kwargs.get("progress_callback"))
            return fake_payload

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "pcb_qa.cli",
                    "agent-ask",
                    "--project-root",
                    ".",
                    "--question",
                    "q",
                    "--quiet",
                ],
            ),
            mock.patch("pcb_qa.cli.run_evidence_agent", side_effect=_fake_run_evidence_agent),
            redirect_stdout(fake_stdout),
            redirect_stderr(fake_stderr),
        ):
            rc = cli.main()

        self.assertEqual(rc, 0)
        self.assertEqual(fake_stderr.getvalue().strip(), "")
        payload = json.loads(fake_stdout.getvalue())
        self.assertIn("question", payload)

    def test_progress_reporter_starts_and_stops_spinner_for_llm_wait(self) -> None:
        reporter = cli._AgentAskProgressReporter(quiet=False)
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
