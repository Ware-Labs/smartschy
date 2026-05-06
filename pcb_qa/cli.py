from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time

from .evidence_agent import AgentLimits, AnswerOptions, run_evidence_agent
from .mcp_server import main as run_mcp_server
from .ingest import ingest_project
from .validation import run_validation


class _AgentAskProgressReporter:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self._spinner_thread: threading.Thread | None = None
        self._spinner_stop = threading.Event()
        self._spinner_running = False
        self._spinner_lock = threading.Lock()

    def callback(self, message: str) -> None:
        if self.quiet:
            return
        if message.startswith("Submitting LLM request"):
            self._print_line(message)
            self._start_spinner()
            return
        self._stop_spinner()
        self._print_line(message)

    def close(self) -> None:
        self._stop_spinner()

    def _print_line(self, message: str) -> None:
        print(f"[agent-ask] {message}", file=sys.stderr, flush=True)

    def _start_spinner(self) -> None:
        with self._spinner_lock:
            if self._spinner_running:
                return
            self._spinner_stop.clear()
            self._spinner_running = True
            self._spinner_thread = threading.Thread(target=self._spin, daemon=True)
            self._spinner_thread.start()

    def _stop_spinner(self) -> None:
        with self._spinner_lock:
            if not self._spinner_running:
                return
            self._spinner_stop.set()
            thread = self._spinner_thread
        if thread is not None:
            thread.join(timeout=0.2)
        with self._spinner_lock:
            self._spinner_running = False
            self._spinner_thread = None
        self._clear_spinner_line()

    def _spin(self) -> None:
        frames = "|/-\\"
        index = 0
        while not self._spinner_stop.is_set():
            frame = frames[index % len(frames)]
            sys.stderr.write(f"\r[agent-ask] Waiting for LLM response {frame}")
            sys.stderr.flush()
            index += 1
            time.sleep(0.1)

    def _clear_spinner_line(self) -> None:
        sys.stderr.write("\r" + (" " * 80) + "\r")
        sys.stderr.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Precision PCB QA pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_cmd = sub.add_parser("ingest", help="Build all local indices.")
    ingest_cmd.add_argument("--project-root", type=Path, required=True)
    ingest_cmd.add_argument("--llm-enrich", action="store_true", help="Enable optional LLM semantic enrichment during ingest.")
    ingest_cmd.add_argument("--llm-model", type=str, default="gpt-5-mini")

    validate_cmd = sub.add_parser("validate", help="Run validation harness.")
    validate_cmd.add_argument("--project-root", type=Path, required=True)

    agent_ask_cmd = sub.add_parser(
        "agent-ask",
        help="Iteratively build evidence packet via LLM-planned tool calls with coverage gating.",
    )
    agent_ask_cmd.add_argument("--project-root", type=Path, required=True)
    agent_ask_cmd.add_argument("--question", type=str, required=True)
    agent_ask_cmd.add_argument("--max-iterations", type=int, default=18)
    agent_ask_cmd.add_argument("--max-tool-calls", type=int, default=120)
    agent_ask_cmd.add_argument("--max-chunks", type=int, default=16)
    agent_ask_cmd.add_argument("--max-schematic-images", type=int, default=4)
    agent_ask_cmd.add_argument("--max-total-evidence-items", type=int, default=64)
    agent_ask_cmd.add_argument("--answer-with-llm", action="store_true")
    agent_ask_cmd.add_argument("--model", type=str, default="gpt-5")
    agent_ask_cmd.add_argument("--image-detail", choices=["auto", "low", "high"], default="auto")
    agent_ask_cmd.add_argument("--quiet", action="store_true", help="Suppress progress output on stderr.")

    sub.add_parser("mcp-server", help="Run local deterministic PCB QA MCP server.")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "ingest":
        payload = ingest_project(
            args.project_root,
            llm_enrich=bool(args.llm_enrich),
            llm_model=str(args.llm_model),
        )
    elif args.command == "validate":
        payload = run_validation(args.project_root)
    elif args.command == "agent-ask":
        reporter = _AgentAskProgressReporter(quiet=args.quiet)
        limits = AgentLimits(
            max_iterations=args.max_iterations,
            max_tool_calls=args.max_tool_calls,
            max_chunks=args.max_chunks,
            max_schematic_images=args.max_schematic_images,
            max_total_evidence_items=args.max_total_evidence_items,
        )
        try:
            payload = run_evidence_agent(
                project_root=args.project_root,
                question=args.question,
                limits=limits,
                answer_options=AnswerOptions(
                    answer_with_llm=args.answer_with_llm,
                    model=args.model,
                    max_schematic_images_for_answer=args.max_schematic_images,
                    image_detail=args.image_detail,
                ),
                progress_callback=None if args.quiet else reporter.callback,
            )
        finally:
            reporter.close()
    elif args.command == "mcp-server":
        return run_mcp_server()
    else:  # pragma: no cover
        parser.error(f"Unsupported command: {args.command}")
        return 2

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
