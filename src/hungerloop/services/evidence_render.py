"""Prompt-safe one-line renderers for persisted evidence."""
from __future__ import annotations

from hungerloop.models.validation import CheckResult

DEFAULT_FAILED_CHECK_CHARS = 3000


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


def clip_head_tail(text: str, max_chars: int, *, head_fraction: float = 0.4) -> str:
    """Keep the head and tail; drop the middle.

    Pytest, ruff, and mypy all put the actionable summary (FAILED lines,
    final stats) at the END of stdout while keeping a setup banner at the
    START. Middle-clipping cuts out exactly the names of failed tests.
    Head+tail keeps both edges where the signal lives.
    """
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    sep = "\n…\n"
    budget = max_chars - len(sep)
    head_chars = max(1, int(budget * head_fraction))
    tail_chars = max(1, budget - head_chars)
    return f"{text[:head_chars]}{sep}{text[-tail_chars:]}"


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


def summarize_failed_tool_call(
    payload: dict[str, object],
    loop_id: int,
    *,
    repeat_count: int = 1,
    max_chars: int = 200,
) -> str:
    """Format one FAILED tool_call evidence row for prompt history.

    ``repeat_count`` > 1 marks byte-identical (tool_name, args) failures —
    the strongest cross-loop signal that a mechanical step keeps failing
    the same way and needs a different edit, not a different approach.
    """
    tool_name = str(payload.get("tool_name", "?"))
    location = _tool_location_hint(payload)
    prefix = f"loop {loop_id} FAILED tool_call {tool_name}"
    if location:
        prefix += f" {location}"
    if repeat_count > 1:
        prefix += f" (x{repeat_count} identical failures)"
    prefix += ": "
    result = str(payload.get("result_summary", ""))
    return _clip_line(f"{prefix}{result}", max_chars)


def summarize_failed_check(
    check_result: CheckResult,
    loop_id: int,
    *,
    max_chars: int = DEFAULT_FAILED_CHECK_CHARS,
) -> str:
    """Format one failed validation check for prompt history.

    Uses head+tail clipping so the pytest/ruff/mypy "FAILED" summary at the
    end of stdout survives the budget — middle-clip used to drop exactly the
    lines naming the failed tests.
    """
    prefix = (
        f"loop {loop_id}: {check_result.check_key} "
        f"{check_result.check_type.value} → "
    )
    detail_budget = max(1, max_chars - len(prefix))
    detail = clip_head_tail(check_result.detail or "", detail_budget)
    line = f"{prefix}{detail}"
    if len(line) <= max_chars:
        return line
    return f"{line[: max_chars - 1]}…"


def _tool_location_hint(payload: dict[str, object]) -> str:
    args_summary = str(payload.get("args_summary", ""))
    if not args_summary.startswith("path="):
        return ""
    path = args_summary.split(" ", 1)[0][len("path=") :]
    return path
