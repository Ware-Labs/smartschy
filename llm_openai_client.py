#!/usr/bin/env python3
"""Small OpenAI chat completions client helper."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClientError(RuntimeError):
    pass


def _post_json(url: str, payload: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def chat_completion(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 1800,
    retries: int = 2,
) -> str:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise OpenAIClientError(
            "OPENAI_API_KEY is not set. Export it or pass api_key explicitly."
        )

    def make_payload(use_max_completion_tokens: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if use_max_completion_tokens:
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
        return payload

    # Start with best guess based on model family.
    use_max_completion_tokens = model.lower().startswith("gpt-5")
    payload = make_payload(use_max_completion_tokens)

    attempt = 0
    while True:
        try:
            data = _post_json(OPENAI_API_URL, payload, key)
            choices = data.get("choices", [])
            if not choices:
                raise OpenAIClientError(f"No choices in response: {data}")
            msg = choices[0].get("message", {})
            text = msg.get("content", "")
            if not text:
                raise OpenAIClientError(f"Missing message content: {data}")
            return str(text)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # Some models require max_completion_tokens instead of max_tokens.
            if "Unsupported parameter" in body:
                if "'max_tokens'" in body and "'max_completion_tokens'" in body:
                    use_max_completion_tokens = True
                    payload = make_payload(use_max_completion_tokens)
                    if attempt < retries:
                        attempt += 1
                        continue
                elif "'max_completion_tokens'" in body and "'max_tokens'" in body:
                    use_max_completion_tokens = False
                    payload = make_payload(use_max_completion_tokens)
                    if attempt < retries:
                        attempt += 1
                        continue
            if attempt >= retries:
                raise OpenAIClientError(
                    f"OpenAI HTTP error {exc.code}: {body}"
                ) from exc
        except (urllib.error.URLError, TimeoutError, OpenAIClientError) as exc:
            if attempt >= retries:
                raise
        attempt += 1
        time.sleep(1.5 * attempt)
