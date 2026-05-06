from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import write_json


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class ToolTraceRecorder:
    output_dir: Path
    tool_calls_path: Path = field(init=False)
    trace_path: Path = field(init=False)
    _tool_call_count: int = field(default=0, init=False)
    _iterations: list[dict[str, Any]] = field(default_factory=list, init=False)
    _model_turns: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tool_calls_path = self.output_dir / "agent_tool_calls.jsonl"
        self.trace_path = self.output_dir / "agent_trace.json"
        self.tool_calls_path.write_text("", encoding="utf-8")

    def record_tool_call(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        self._tool_call_count += 1
        call_id = f"call_{self._tool_call_count:04d}"
        row = {
            "tool_call_id": call_id,
            "timestamp_utc": _utc_now_iso(),
            "tool_name": name,
            "args": args,
            "result": result,
        }
        with self.tool_calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")
        return call_id

    def record_iteration(
        self,
        iteration: int,
        plan: list[dict[str, Any]],
        tool_call_ids: list[str],
        sufficiency: dict[str, Any],
    ) -> None:
        self._iterations.append(
            {
                "iteration": iteration,
                "timestamp_utc": _utc_now_iso(),
                "plan": plan,
                "tool_call_ids": tool_call_ids,
                "sufficiency": sufficiency,
            }
        )

    def record_model_turn(
        self,
        iteration: int,
        response_id: str,
        finish_reason: str,
        requested_tools: list[str],
    ) -> None:
        self._model_turns.append(
            {
                "iteration": iteration,
                "timestamp_utc": _utc_now_iso(),
                "response_id": response_id,
                "finish_reason": finish_reason,
                "requested_tools": requested_tools,
            }
        )

    def write_trace(self, limits: dict[str, Any], stop_reason: str, summary: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "generated_at_utc": _utc_now_iso(),
            "limits": limits,
            "stop_reason": stop_reason,
            "tool_call_count": self._tool_call_count,
            "iterations": self._iterations,
            "model_turns": self._model_turns,
            "summary": summary,
            "tool_calls_path": str(self.tool_calls_path),
        }
        write_json(self.trace_path, payload)
        return payload
