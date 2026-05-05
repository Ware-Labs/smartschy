from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from openai import OpenAI
from openai import OpenAIError
from dotenv import load_dotenv

from pcb_qa.qa import answer_question


DEFAULT_QUESTION = "did I connect the crystal correctly to the microcontroller?"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate evidence and ask OpenAI for an answer.")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--question", type=str, default=DEFAULT_QUESTION)
    parser.add_argument("--model", type=str, default="gpt5.4")
    parser.add_argument("--net-walk-depth", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=6)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    project_root = args.project_root.resolve()

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")

    qa_summary = answer_question(
        project_root=project_root,
        question=args.question,
        net_walk_depth=args.net_walk_depth,
        top_k=args.top_k,
    )

    prompt_path = Path(qa_summary["prompt_path"])
    if not prompt_path.exists():
        raise SystemExit(f"Prompt file not found at {prompt_path}. Run ingest/ask first.")
    prompt = prompt_path.read_text(encoding="utf-8")

    client = OpenAI(api_key=api_key)
    try:
        response = client.responses.create(
            model=args.model,
            input=prompt,
        )
    except OpenAIError as exc:
        raise SystemExit(
            f"OpenAI request failed for model '{args.model}'. "
            f"Verify the model id and API access. Details: {exc}"
        ) from exc

    answer_text = response.output_text.strip()
    answer_path = project_root / "derived" / "qa" / "last_answer.txt"
    answer_path.write_text(answer_text, encoding="utf-8")

    payload = {
        "question": args.question,
        "model": args.model,
        "prompt_path": str(prompt_path),
        "answer_path": str(answer_path),
        "answer_preview": answer_text[:500],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
