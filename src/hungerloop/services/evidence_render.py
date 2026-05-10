"""Prompt-safe one-line renderers for persisted evidence."""
from __future__ import annotations

from hungerloop.models.validation import CheckResult

DEFAULT_FAILED_CHECK_CHARS = 500


def _clip_line(line: str, max_chars: int) -> str:
    if len(line) <= max_chars:
        return line
    return f"{line[: max_chars - 1]}…"


def _clip_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    head_chars = max(1, (max_chars - 1) // 2)
    tail_chars = max(1, max_chars - 1 - head_chars)
    return f"{text[:head_chars]}…{text[-tail_chars:]}"


def summarize_tool_call(
    payload: dict[str, object],
    loop_id: int,
    *,
    max_chars: int = 200,
) -> str:
    """Format one successful tool_call evidence row for prompt history."""
    tool_name = str(payload.get("tool_name", "?"))
    location = _tool_location_hint(payload)
    prefix = f"loop {loop_id} tool_call {tool_name}"
    if location:
        prefix += f" {location}"
    prefix += ": "
    result = str(payload.get("result_summary", ""))
    if tool_name == "read_file":
        result_summary = _clip_middle(result, max(1, max_chars - len(prefix)))
    else:
        result_summary = result[:80]
    return _clip_line(f"{prefix}{result_summary}", max_chars)


def summarize_failed_check(
    check_result: CheckResult,
    loop_id: int,
    *,
    max_chars: int = DEFAULT_FAILED_CHECK_CHARS,
) -> str:
    """Format one failed validation check for prompt history."""
    prefix = (
        f"loop {loop_id}: {check_result.check_key} "
        f"{check_result.check_type.value} → "
    )
    detail_budget = max(1, max_chars - len(prefix))
    detail = _clip_middle(check_result.detail or "", detail_budget)
    line = f"{prefix}{detail}"
    return _clip_line(line, max_chars)


def _tool_location_hint(payload: dict[str, object]) -> str:
    args_summary = str(payload.get("args_summary", ""))
    if not args_summary.startswith("path="):
        return ""
    path = args_summary.split(" ", 1)[0][len("path=") :]
    return path
