from __future__ import annotations

import argparse
import base64
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
    parser.add_argument("--resolver-mode", choices=["config", "legacy"], default="config")
    parser.add_argument("--max-schematic-images", type=int, default=4)
    parser.add_argument("--image-detail", choices=["auto", "low", "high"], default="auto")
    return parser.parse_args()


def _load_schematic_image_manifest(project_root: Path) -> dict:
    manifest_path = project_root / "derived" / "pdf" / "schematic_page_images.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _build_image_lookup(manifest: dict, manifest_base: Path) -> dict[int, Path]:
    lookup: dict[int, Path] = {}
    for row in manifest.get("images", []):
        page_number = row.get("page_number")
        rel_path = row.get("image_path")
        if isinstance(page_number, int) and isinstance(rel_path, str):
            lookup[page_number] = manifest_base / rel_path
    return lookup


def _build_multimodal_content(
    prompt: str,
    page_numbers: list[int],
    image_lookup: dict[int, Path],
    max_images: int,
    image_detail: str,
    warnings: list[str],
) -> tuple[list[dict], list[int]]:
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    used_pages: list[int] = []
    for page_number in page_numbers[:max(0, max_images)]:
        image_path = image_lookup.get(page_number)
        if image_path is None or not image_path.exists():
            warnings.append(f"missing_schematic_image_page_{page_number}")
            continue
        image_bytes = image_path.read_bytes()
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {
                "type": "input_text",
                "text": f"Schematic page {page_number} from the relevant schematic document.",
            }
        )
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{image_b64}",
                "detail": image_detail,
            }
        )
        used_pages.append(page_number)
    return content, used_pages


def main() -> int:
    args = _parse_args()
    project_root = args.project_root.resolve()
    warnings: list[str] = []

    load_dotenv(project_root / ".env")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in environment or .env file.")

    qa_summary = answer_question(
        project_root=project_root,
        question=args.question,
        net_walk_depth=args.net_walk_depth,
        top_k=args.top_k,
        resolver_mode=args.resolver_mode,
    )

    prompt_path = Path(qa_summary["prompt_path"])
    if not prompt_path.exists():
        raise SystemExit(f"Prompt file not found at {prompt_path}. Run ingest/ask first.")
    prompt = prompt_path.read_text(encoding="utf-8")
    relevant_pages = list(qa_summary.get("relevant_schematic_pages", []))

    manifest = _load_schematic_image_manifest(project_root)
    image_lookup = _build_image_lookup(manifest, project_root / "derived" / "pdf")
    content, used_pages = _build_multimodal_content(
        prompt=prompt,
        page_numbers=[int(p) for p in relevant_pages if isinstance(p, int)],
        image_lookup=image_lookup,
        max_images=args.max_schematic_images,
        image_detail=args.image_detail,
        warnings=warnings,
    )
    use_multimodal = len(used_pages) > 0

    client = OpenAI(api_key=api_key)
    try:
        if use_multimodal:
            response = client.responses.create(
                model=args.model,
                input=[{"role": "user", "content": content}],
            )
        else:
            if relevant_pages:
                warnings.append("no_schematic_images_attached_fallback_to_text")
            response = client.responses.create(
                model=args.model,
                input=prompt,
            )
    except OpenAIError as exc:
        if use_multimodal:
            warnings.append("multimodal_request_failed_retry_text_only")
            try:
                response = client.responses.create(
                    model=args.model,
                    input=prompt,
                )
            except OpenAIError as retry_exc:
                raise SystemExit(
                    f"OpenAI request failed for model '{args.model}'. "
                    f"Verify the model id and API access. Details: {retry_exc}"
                ) from retry_exc
        else:
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
        "resolver_mode": args.resolver_mode,
        "prompt_path": str(prompt_path),
        "answer_path": str(answer_path),
        "relevant_schematic_pages": relevant_pages,
        "attached_schematic_pages": used_pages,
        "image_detail": args.image_detail,
        "multimodal_used": use_multimodal,
        "warnings": warnings,
        "answer_preview": answer_text[:500],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
