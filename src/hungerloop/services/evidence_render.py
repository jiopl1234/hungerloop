"""Prompt-safe one-line renderers for persisted evidence."""
from __future__ import annotations

from hungerloop.models.validation import CheckResult


def _clip_line(line: str, max_chars: int) -> str:
    if len(line) <= max_chars:
        return line
    return f"{line[: max_chars - 1]}…"


def summarize_tool_call(
    payload: dict[str, object],
    loop_id: int,
    *,
    max_chars: int = 200,
) -> str:
    """Format one successful tool_call evidence row for prompt history."""
    tool_name = str(payload.get("tool_name", "?"))
    result_summary = str(payload.get("result_summary", ""))[:80]
    return _clip_line(
        f"loop {loop_id} tool_call {tool_name}: {result_summary}",
        max_chars,
    )


def summarize_failed_check(
    check_result: CheckResult,
    loop_id: int,
    *,
    max_chars: int = 200,
) -> str:
    """Format one failed validation check for prompt history."""
    detail = (check_result.detail or "")[:120]
    line = (
        f"loop {loop_id}: {check_result.check_key} "
        f"{check_result.check_type.value} → {detail}"
    )
    return _clip_line(line, max_chars)
