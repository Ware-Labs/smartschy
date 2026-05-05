from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evidence_agent import AgentLimits, AnswerOptions, run_evidence_agent
from .mcp_server import main as run_mcp_server
from .ingest import ingest_project
from .qa import answer_question
from .validation import run_validation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Precision PCB QA pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_cmd = sub.add_parser("ingest", help="Build all local indices.")
    ingest_cmd.add_argument("--project-root", type=Path, required=True)

    ask_cmd = sub.add_parser("ask", help="Build evidence packet and prompt for a question.")
    ask_cmd.add_argument("--project-root", type=Path, required=True)
    ask_cmd.add_argument("--question", type=str, required=True)
    ask_cmd.add_argument("--net-walk-depth", type=int, default=1)
    ask_cmd.add_argument("--top-k", type=int, default=6)
    ask_cmd.add_argument("--resolver-mode", choices=["config", "legacy"], default="config")

    validate_cmd = sub.add_parser("validate", help="Run validation harness.")
    validate_cmd.add_argument("--project-root", type=Path, required=True)
    validate_cmd.add_argument("--resolver-mode", choices=["config", "legacy"], default="config")

    agent_ask_cmd = sub.add_parser(
        "agent-ask",
        help="Iteratively build evidence packet and strict prompt via deterministic tools.",
    )
    agent_ask_cmd.add_argument("--project-root", type=Path, required=True)
    agent_ask_cmd.add_argument("--question", type=str, required=True)
    agent_ask_cmd.add_argument("--max-iterations", type=int, default=6)
    agent_ask_cmd.add_argument("--max-tool-calls", type=int, default=40)
    agent_ask_cmd.add_argument("--max-chunks", type=int, default=16)
    agent_ask_cmd.add_argument("--max-schematic-images", type=int, default=4)
    agent_ask_cmd.add_argument("--max-total-evidence-items", type=int, default=64)
    agent_ask_cmd.add_argument("--answer-with-llm", action="store_true")
    agent_ask_cmd.add_argument("--model", type=str, default="gpt-5")
    agent_ask_cmd.add_argument("--image-detail", choices=["auto", "low", "high"], default="auto")

    sub.add_parser("mcp-server", help="Run local deterministic PCB QA MCP server.")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "ingest":
        payload = ingest_project(args.project_root)
    elif args.command == "ask":
        payload = answer_question(
            args.project_root,
            args.question,
            net_walk_depth=args.net_walk_depth,
            top_k=args.top_k,
            resolver_mode=args.resolver_mode,
        )
    elif args.command == "validate":
        payload = run_validation(args.project_root, resolver_mode=args.resolver_mode)
    elif args.command == "agent-ask":
        limits = AgentLimits(
            max_iterations=args.max_iterations,
            max_tool_calls=args.max_tool_calls,
            max_chunks=args.max_chunks,
            max_schematic_images=args.max_schematic_images,
            max_total_evidence_items=args.max_total_evidence_items,
        )
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
        )
    elif args.command == "mcp-server":
        return run_mcp_server()
    else:  # pragma: no cover
        parser.error(f"Unsupported command: {args.command}")
        return 2

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
