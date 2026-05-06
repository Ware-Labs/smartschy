from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pcb_qa.evidence_agent import AgentLimits, AnswerOptions, run_evidence_agent


DEFAULT_QUESTION = "did I connect the crystal correctly to the microcontroller?"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate evidence and ask OpenAI for an answer.")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--question", type=str, default=DEFAULT_QUESTION)
    parser.add_argument("--model", type=str, default="gpt-5")
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--max-tool-calls", type=int, default=40)
    parser.add_argument("--max-chunks", type=int, default=16)
    parser.add_argument("--max-schematic-images", type=int, default=4)
    parser.add_argument("--image-detail", choices=["auto", "low", "high"], default="auto")
    return parser.parse_args()


def _stderr_progress(message: str) -> None:
    print(f"[ask_and_answer] {message}", file=sys.stderr, flush=True)


def main() -> int:
    args = _parse_args()
    project_root = args.project_root.resolve()
    payload = run_evidence_agent(
        project_root=project_root,
        question=args.question,
        limits=AgentLimits(
            max_iterations=args.max_iterations,
            max_tool_calls=args.max_tool_calls,
            max_chunks=args.max_chunks,
            max_schematic_images=args.max_schematic_images,
            max_total_evidence_items=64,
        ),
        answer_options=AnswerOptions(
            answer_with_llm=True,
            model=args.model,
            max_schematic_images_for_answer=args.max_schematic_images,
            image_detail=args.image_detail,
        ),
        progress_callback=_stderr_progress,
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
