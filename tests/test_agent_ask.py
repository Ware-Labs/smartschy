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
        REPO_ROOT / "derived" / "pdf" / "schematic_page_images.json",
    ]
    return all(path.exists() for path in required)


@unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
class AgentAskIntegrationTests(unittest.TestCase):
    def test_agent_ask_builds_packet_and_prompt(self) -> None:
        summary = run_evidence_agent(
            project_root=REPO_ROOT,
            question="is VDDIO connected correctly to the ICM-42605?",
            limits=AgentLimits(
                max_iterations=3,
                max_tool_calls=30,
                max_chunks=8,
                max_schematic_images=2,
                max_total_evidence_items=20,
            ),
        )
        self.assertGreater(summary["evidence_item_count"], 0)
        packet_path = Path(summary["evidence_packet_path"])
        prompt_path = Path(summary["prompt_path"])
        self.assertTrue(packet_path.exists())
        self.assertTrue(prompt_path.exists())

        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertIn("open_uncertainties", packet)
        self.assertGreater(len(packet["open_uncertainties"]), 0)
        self.assertIn("critical_findings", packet)
        self.assertTrue(
            any("U3-5 is floating" in finding for finding in packet.get("critical_findings", [])),
            "Expected critical finding for U3 pin 5 floating.",
        )
        self.assertIn("required_pin_floating:U3-5", packet.get("open_uncertainties", []))

        has_dsn = any(item.get("source_priority") == "DSN" for item in packet.get("evidence", []))
        self.assertTrue(has_dsn, "Expected DSN-priority evidence for connectivity question")

        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Critical DSN Findings:", prompt)
        self.assertIn("U3-5 is floating", prompt)
        self.assertIn("Citations:", prompt)
        self.assertIn("Never invent values", prompt)
        self.assertIn("Uncertainty:", prompt)

    def test_agent_ask_relationship_question_keeps_broad_coverage(self) -> None:
        summary = run_evidence_agent(
            project_root=REPO_ROOT,
            question="how does the bluetooth controller communicate with the imu?",
            limits=AgentLimits(
                max_iterations=3,
                max_tool_calls=35,
                max_chunks=10,
                max_schematic_images=3,
                max_total_evidence_items=30,
            ),
        )
        packet = json.loads(Path(summary["evidence_packet_path"]).read_text(encoding="utf-8"))
        resolved = packet.get("resolved_entities", {})
        self.assertGreaterEqual(len(resolved.get("components", [])), 2)
        self.assertTrue(any(ref.startswith("U") or ref.startswith("MOD") for ref in resolved.get("components", [])))
        self.assertTrue(any(item.get("type") == "trace_net_neighborhood" for item in packet.get("evidence", [])))


if __name__ == "__main__":
    unittest.main()
