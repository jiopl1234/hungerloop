"""``is_reusable`` regex corpus test (PRD §15 / FR-8 / FR-9).

Pins two things:

1. A 10+ string corpus of *real-world* HungerLoop output strings
   (LoopTrace ``delta_summary``, accepted-check messages, StopReport
   recommendations) is accepted as reusable. The false-positive rate
   on this corpus is zero.
2. Each :data:`TASK_SPECIFIC_PATTERNS` entry rejects at least one
   well-formed example so a future refactor doesn't quietly drop a
   pattern.
"""
from __future__ import annotations

import pytest

from hungerloop.services.memory_manager import (
    TASK_SPECIFIC_PATTERNS,
    is_reusable,
)

REUSABLE_CORPUS: list[str] = [
    "Verified acceptance check H-001:0",
    "report.md exists with non-empty content",
    "shell command exited with code 0",
    "patch applied cleanly to existing file",
    "validation passed: file_exists check returned True",
    "loop committed: write_file action produced report.md",
    "StopReport: every hunger item satisfied; no pending checks remain.",
    "Task DONE — best_state promoted with evidence count > 0",
    "tool call 'write_file' succeeded with artifact_count=1",
    "model call returned valid JSON; tokens consumed=320, cost=$0.001",
    "BLOCKED items unblocked by operator; resuming from clean state.",
    "Acceptance check H-002:1 passed across two consecutive loops.",
]


NON_REUSABLE_CORPUS: list[tuple[str, int]] = [
    # (string, index of TASK_SPECIFIC_PATTERNS that should match)
    ("processed task_550e8400-e29b-41d4-a716-446655440000 successfully", 0),
    ("loop_007 produced an artifact under loop_008", 1),
    ("/tmp/abc/report.md was written by the worker", 2),
    ("workspace/tasks/abc-task/best/files/x.md", 3),
    ("CAND-12ab promoted to best/", 4),
    ("ValidationReport id=VAL-deadbeef returned verdict=pass", 5),
]


@pytest.mark.parametrize("content", REUSABLE_CORPUS)
def test_corpus_strings_accepted_as_reusable(content: str) -> None:
    assert is_reusable(content), (
        f"corpus string falsely flagged as task-specific: {content!r}"
    )


@pytest.mark.parametrize("content,expected_pattern_idx", NON_REUSABLE_CORPUS)
def test_task_specific_strings_rejected(
    content: str, expected_pattern_idx: int
) -> None:
    assert not is_reusable(content), (
        f"task-specific string falsely accepted as reusable: {content!r}"
    )
    # And the matching pattern is the one we expected (pin against
    # silent reorder of the patterns tuple).
    assert TASK_SPECIFIC_PATTERNS[expected_pattern_idx].search(content), (
        f"expected pattern {expected_pattern_idx} to match {content!r} "
        "but it did not"
    )


def test_pattern_set_size_pinned() -> None:
    """If the pattern set grows or shrinks, the corpus indices need
    to be revisited; this test fails loudly to flag that."""
    assert len(TASK_SPECIFIC_PATTERNS) == 6
