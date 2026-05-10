"""Tests for prompt-safe evidence renderers."""
from __future__ import annotations

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.validation import CheckResult
from hungerloop.services.evidence_render import (
    summarize_failed_check,
    summarize_tool_call,
)


def test_summarize_failed_check_preserves_long_detail_head_and_tail() -> None:
    check = CheckResult(
        hunger_item_id="H-001",
        check_index=0,
        check_key="H-001:0",
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        passed=False,
        detail="exit=1; stderr=Traceback head " + ("x" * 600) + " final assertion tail",
    )

    line = summarize_failed_check(check, loop_id=2, max_chars=220)

    assert len(line) <= 220
    assert "Traceback head" in line
    assert "final assertion tail" in line
    assert "…" in line


def test_summarize_read_file_tool_call_preserves_head_and_tail() -> None:
    line = summarize_tool_call(
        {
            "tool_name": "read_file",
            "args_summary": "path=logkit/stats.py",
            "result_summary": "def compute_stats():\n" + ("x" * 600) + "timestamp.strftime()",
        },
        loop_id=3,
        max_chars=220,
    )

    assert len(line) <= 220
    assert "read_file logkit/stats.py" in line
    assert "def compute_stats" in line
    assert "timestamp.strftime()" in line
    assert "…" in line
