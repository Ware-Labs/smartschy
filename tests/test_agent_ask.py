from __future__ import annotations

import json
import unittest
from unittest import mock
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


class _FakeResponse:
    def __init__(self, response_id: str, output: list[dict], output_text: str = "") -> None:
        self.id = response_id
        self.output = output
        self.output_text = output_text


class _FakeResponsesApi:
    def __init__(self) -> None:
        self._call_count = 0

    def create(self, **kwargs: object) -> _FakeResponse:
        self._call_count += 1
        previous_response_id = kwargs.get("previous_response_id")
        if previous_response_id is None:
            question = str(kwargs.get("input", ""))
            if "bluetooth controller" in question.lower():
                return _FakeResponse(
                    "resp_1",
                    [
                        {
                            "type": "function_call",
                            "name": "search_components",
                            "call_id": "mcp_1",
                            "arguments": json.dumps({"query": "bluetooth controller imu"}),
                        },
                        {
                            "type": "function_call",
                            "name": "trace_net_neighborhood",
                            "call_id": "mcp_2",
                            "arguments": json.dumps({"seed_components": ["U3", "U13"], "depth": 1, "max_nodes": 120}),
                        },
                        {
                            "type": "function_call",
                            "name": "get_component_pins",
                            "call_id": "mcp_3",
                            "arguments": json.dumps({"refdes": "U3"}),
                        },
                    ],
                )
            return _FakeResponse(
                "resp_1",
                [
                    {
                        "type": "function_call",
                        "name": "search_components",
                        "call_id": "mcp_1",
                        "arguments": json.dumps({"query": "ICM-42605 VDDIO"}),
                    },
                    {
                        "type": "function_call",
                        "name": "get_component_pins",
                        "call_id": "mcp_2",
                        "arguments": json.dumps({"refdes": "U3"}),
                    },
                    {
                        "type": "function_call",
                        "name": "get_schematic_pages",
                        "call_id": "mcp_3",
                        "arguments": json.dumps({"query": "VDDIO ICM-42605", "max_results": 2}),
                    },
                ],
            )

        # Finalize turn for evidence-building loop.
        tool_outputs = kwargs.get("input", [])
        selected_ids: list[str] = []
        if isinstance(tool_outputs, list):
            for item in tool_outputs:
                if not isinstance(item, dict):
                    continue
                raw_output = item.get("output", "{}")
                try:
                    decoded = json.loads(str(raw_output))
                except Exception:
                    decoded = {}
                tool_call_id = decoded.get("tool_call_id")
                if isinstance(tool_call_id, str):
                    selected_ids.append(tool_call_id)
        return _FakeResponse(
            "resp_2",
            [
                {
                    "type": "function_call",
                    "name": "finalize_evidence",
                    "call_id": "mcp_finalize",
                    "arguments": json.dumps(
                        {
                            "selected_tool_call_ids": selected_ids,
                            "resolved_entities": {"components": ["U3"], "nets": ["NETC18_1"]},
                            "open_uncertainties": ["pin_function_mapping_unresolved"],
                            "stop_reason": "model_finalize",
                        }
                    ),
                }
            ],
        )


class _FakeOpenAI:
    def __init__(self, api_key: str) -> None:
        _ = api_key
        self.responses = _FakeResponsesApi()


@unittest.skipUnless(_has_required_artifacts(), "Derived artifacts are not available")
class AgentAskIntegrationTests(unittest.TestCase):
    def test_agent_ask_builds_packet_and_prompt(self) -> None:
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch("pcb_qa.evidence_agent.OpenAI", _FakeOpenAI),
        ):
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
        self.assertEqual(summary["stop_reason"], "model_finalize")
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
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch("pcb_qa.evidence_agent.OpenAI", _FakeOpenAI),
        ):
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
        self.assertGreaterEqual(len(resolved.get("components", [])), 1)
        self.assertTrue(any(ref.startswith("U") or ref.startswith("MOD") for ref in packet.get("resolved_entities", {}).get("components", [])))
        self.assertTrue(any(item.get("type") == "trace_net_neighborhood" for item in packet.get("evidence", [])))
        trace = json.loads(Path(summary["agent_trace_path"]).read_text(encoding="utf-8"))
        self.assertGreater(len(trace.get("model_turns", [])), 0)

    def test_agent_ask_emits_progress_messages_via_callback(self) -> None:
        progress_events: list[str] = []
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch("pcb_qa.evidence_agent.OpenAI", _FakeOpenAI),
        ):
            run_evidence_agent(
                project_root=REPO_ROOT,
                question="is VDDIO connected correctly to the ICM-42605?",
                limits=AgentLimits(
                    max_iterations=2,
                    max_tool_calls=24,
                    max_chunks=8,
                    max_schematic_images=2,
                    max_total_evidence_items=24,
                ),
                progress_callback=progress_events.append,
            )
        self.assertGreater(len(progress_events), 0)
        self.assertTrue(any("Iteration" in msg for msg in progress_events))
        self.assertTrue(any("Calling tool" in msg for msg in progress_events))


if __name__ == "__main__":
    unittest.main()
