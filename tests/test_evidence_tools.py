from __future__ import annotations

import unittest
from pathlib import Path

from pcb_qa import evidence_tools
from pcb_qa.prompt_render import render_prompt_from_evidence_packet


REPO_ROOT = Path(__file__).resolve().parents[1]


def _has_required_artifacts() -> bool:
    required = [
        REPO_ROOT / "derived" / "dsn" / "pin_to_net.json",
        REPO_ROOT / "derived" / "dsn" / "nets.jsonl",
        REPO_ROOT / "derived" / "pdf" / "pdf_chunks.jsonl",
    ]
    return all(path.exists() for path in required)


@unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
class EvidenceToolTests(unittest.TestCase):
    def test_get_pin_net_exact_connection(self) -> None:
        payload = evidence_tools.get_pin_net(REPO_ROOT, refdes="U3", pin="8")
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["net_name_canonical"], "NETC18_1")

    def test_get_net_members_completeness(self) -> None:
        payload = evidence_tools.get_net_members(REPO_ROOT, net_name="SWDIO")
        grouped = payload["members"]["component_to_pins"]
        self.assertIn("MOD1", grouped)
        self.assertIn("P3", grouped)
        self.assertIn("5", grouped["MOD1"])
        self.assertIn("2", grouped["P3"])

    def test_search_pdf_chunks_has_source_metadata(self) -> None:
        payload = evidence_tools.search_pdf_chunks(
            REPO_ROOT,
            query="ICM-42605 VDDIO",
            source_type="datasheet",
            max_results=3,
        )
        self.assertGreater(len(payload["results"]), 0)
        first = payload["results"][0]
        self.assertIn("source_file", first)
        self.assertIn("chunk_id", first)
        self.assertIn("score", first)

    def test_component_context_bundle_shape(self) -> None:
        payload = evidence_tools.get_component_context_bundle(REPO_ROOT, refdes="U3", max_results=3)
        self.assertEqual(payload["refdes"], "U3")
        self.assertIn("component", payload)
        self.assertIn("pins", payload)
        self.assertIn("top_datasheet_chunks", payload)
        self.assertIn("related_schematic_pages", payload)


class McpServerImportTests(unittest.TestCase):
    def test_mcp_server_can_build_when_dependency_present(self) -> None:
        try:
            from pcb_qa.mcp_server import build_mcp_server
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"MCP server module unavailable: {exc}")
            return
        try:
            server = build_mcp_server()
        except RuntimeError as exc:
            self.skipTest(str(exc))
            return
        self.assertIsNotNone(server)


class PromptRenderTests(unittest.TestCase):
    def test_render_prompt_includes_critical_findings_block(self) -> None:
        packet = {
            "question": "is VDDIO connected correctly to the ICM-42605?",
            "critical_findings": ["DSN: U3-5 is floating (unconnected), contradicting expected VDDIO tie."],
            "evidence": [],
        }
        prompt = render_prompt_from_evidence_packet(packet)
        self.assertIn("Critical DSN Findings:", prompt)
        self.assertIn("U3-5 is floating", prompt)


if __name__ == "__main__":
    unittest.main()
