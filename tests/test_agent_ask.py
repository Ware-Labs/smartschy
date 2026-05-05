from __future__ import annotations

import json
import unittest
from pathlib import Path

from pcb_qa.evidence_agent import AgentLimits, run_evidence_agent


REPO_ROOT = Path(__file__).resolve().parents[1]


def _has_required_artifacts() -> bool:
    required = [
        REPO_ROOT / "derived" / "dsn" / "pin_to_net.json",
        REPO_ROOT / "derived" / "pdf" / "pdf_chunks.jsonl",
        REPO_ROOT / "derived" / "qa" / "connectivity_anomalies.jsonl",
        REPO_ROOT / "derived" / "kg" / "function_blocks.json",
    ]
    return all(path.exists() for path in required)


@unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
class AgentAskIntegrationTests(unittest.TestCase):
    def test_agent_ask_builds_v2_packet_and_prompt(self) -> None:
        summary = run_evidence_agent(
            project_root=REPO_ROOT,
            question="is VDDIO connected correctly to the ICM-42605?",
            limits=AgentLimits(max_chunks=8, max_total_evidence_items=24),
        )
        self.assertGreater(summary["evidence_item_count"], 0)
        self.assertIn(summary["stop_reason"], {"single_mode_complete", "insufficient_breadth"})
        packet_path = Path(summary["evidence_packet_path"])
        prompt_path = Path(summary["prompt_path"])
        self.assertTrue(packet_path.exists())
        self.assertTrue(prompt_path.exists())

        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertIn("evidence_diversity_metrics", packet)
        self.assertIn("intent", packet)
        self.assertIn("critical_findings", packet)
        self.assertTrue(any(item.get("source_priority") == "DSN" for item in packet.get("evidence", [])))

        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Question intent:", prompt)
        self.assertIn("Evidence diversity metrics:", prompt)
        self.assertIn("Citations:", prompt)

    def test_system_function_question_uses_breadth_mode(self) -> None:
        summary = run_evidence_agent(
            project_root=REPO_ROOT,
            question="Explain what this circuit does",
            limits=AgentLimits(max_chunks=10, max_total_evidence_items=32),
        )
        packet = json.loads(Path(summary["evidence_packet_path"]).read_text(encoding="utf-8"))
        self.assertEqual(packet.get("intent"), "system_function")
        resolved = packet.get("resolved_entities", {})
        self.assertGreaterEqual(len(resolved.get("nets", [])), 3)
        metrics = packet.get("evidence_diversity_metrics", {})
        self.assertIn("distinct_nets", metrics)

    def test_agent_ask_emits_progress_messages(self) -> None:
        progress_events: list[str] = []
        run_evidence_agent(
            project_root=REPO_ROOT,
            question="which nets connect the imu signals to the module?",
            limits=AgentLimits(max_chunks=6, max_total_evidence_items=20),
            progress_callback=progress_events.append,
        )
        self.assertGreater(len(progress_events), 0)
        self.assertTrue(any("Starting single-mode agent" in msg for msg in progress_events))
        self.assertTrue(any("Finished in" in msg for msg in progress_events))


if __name__ == "__main__":
    unittest.main()

