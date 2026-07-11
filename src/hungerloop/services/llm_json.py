"""Small shared helpers for parsing JSON-only LLM responses."""
from __future__ import annotations

import json
import re
from typing import Any


def strip_fenced_json(text: str) -> str:
    """Return JSON content from an optional Markdown code fence."""
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    return fence.group(1).strip() if fence else text.strip()


def parse_json_response(text: str) -> Any | None:
    """Parse an optional fenced JSON response, returning ``None`` on failure."""
    stripped = strip_fenced_json(text)
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
