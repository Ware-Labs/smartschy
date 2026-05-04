#!/usr/bin/env python3
"""Optional LLM cleanup stage for rendered datasheet markdown."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from llm_openai_client import OpenAIClientError, chat_completion


def _extract_numeric_tokens(text: str) -> List[str]:
    pattern = re.compile(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")
    return pattern.findall(text)


def _basic_deterministic_cleanup(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: List[str] = []
    for line in lines:
        if cleaned and cleaned[-1] and line and not line.startswith("#") and not line.startswith("|"):
            if cleaned[-1].endswith("-") and not cleaned[-1].endswith("--"):
                cleaned[-1] = cleaned[-1][:-1] + line
                continue
        cleaned.append(line)
    return "\n".join(cleaned).strip() + "\n"


def cleanup_markdown_with_llm(
    markdown: str,
    *,
    normalized_document: Dict[str, Any],
    model: str,
    mock: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Apply constrained cleanup. Falls back to deterministic cleanup on failure."""
    baseline_numbers = _extract_numeric_tokens(markdown)
    if mock:
        cleaned = _basic_deterministic_cleanup(markdown)
        return cleaned, {
            "cleanup_mode": "mock",
            "llm_used": False,
            "numeric_token_preserved": _extract_numeric_tokens(cleaned) == baseline_numbers,
            "warnings": [],
        }

    system_prompt = (
        "You clean markdown formatting for technical datasheets. "
        "Do not invent, delete, or alter numeric values, units, table cells, limits, conditions, or notes. "
        "Do not summarize. Keep headings, tables, and ordering intact. "
        "Only fix awkward line breaks and lightly normalize heading punctuation."
    )
    user_prompt = (
        "Normalize formatting of this markdown while preserving data exactly.\n\n"
        "Context JSON for reference only:\n"
        f"{normalized_document}\n\n"
        "Markdown to clean:\n"
        f"{markdown}"
    )

    warnings: List[str] = []
    try:
        cleaned = chat_completion(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=6000,
        ).strip() + "\n"
    except OpenAIClientError as exc:
        warnings.append(f"LLM cleanup failed: {exc}")
        cleaned = _basic_deterministic_cleanup(markdown)
        return cleaned, {
            "cleanup_mode": "fallback_deterministic",
            "llm_used": False,
            "numeric_token_preserved": _extract_numeric_tokens(cleaned) == baseline_numbers,
            "warnings": warnings,
        }

    cleaned_numbers = _extract_numeric_tokens(cleaned)
    numeric_ok = cleaned_numbers == baseline_numbers
    if not numeric_ok:
        warnings.append("LLM cleanup modified numeric tokens; reverted to deterministic markdown.")
        cleaned = markdown

    return cleaned, {
        "cleanup_mode": "llm",
        "llm_used": True,
        "numeric_token_preserved": numeric_ok,
        "warnings": warnings,
    }
