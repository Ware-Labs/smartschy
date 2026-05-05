from __future__ import annotations

import argparse
import json
from pathlib import Path

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

    validate_cmd = sub.add_parser("validate", help="Run validation harness.")
    validate_cmd.add_argument("--project-root", type=Path, required=True)

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
        )
    elif args.command == "validate":
        payload = run_validation(args.project_root)
    else:  # pragma: no cover
        parser.error(f"Unsupported command: {args.command}")
        return 2

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
