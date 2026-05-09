"""Shared rendering for the worker prior-loop context block.

Both ``ContextBuilder`` history-cap accounting and ``ExecutionWorker`` prompt
assembly must call this module so the measured block matches the emitted text.
"""
from __future__ import annotations

from collections.abc import Sequence


def has_prior_loop_context(
    *,
    last_self_summary: str | None,
    best_workspace_files: Sequence[str],
    failure_patterns_to_avoid: Sequence[str],
    relevant_evidence_summaries: Sequence[str],
) -> bool:
    """Return whether the prior-loop block should be rendered."""
    return (
        last_self_summary is not None
        or bool(failure_patterns_to_avoid)
        or bool(relevant_evidence_summaries)
        or bool(best_workspace_files)
    )


def render_prior_loop_context_block(
    *,
    loop_id: int,
    last_self_summary: str | None,
    best_state_summary: str | None,
    best_workspace_files: Sequence[str],
    failure_patterns_to_avoid: Sequence[str],
    relevant_evidence_summaries: Sequence[str],
) -> str:
    """Render the exact prompt block shared by builder and worker."""
    if not has_prior_loop_context(
        last_self_summary=last_self_summary,
        best_workspace_files=best_workspace_files,
        failure_patterns_to_avoid=failure_patterns_to_avoid,
        relevant_evidence_summaries=relevant_evidence_summaries,
    ):
        return ""

    lines = [
        f"Prior loop context (loop {loop_id} of this task):",
        f"- last attempt summary: {last_self_summary or 'n/a'}",
        f"- best state: {best_state_summary or 'empty'}",
    ]
    if best_workspace_files:
        lines.append(f"- files in best/: {', '.join(best_workspace_files)}")
    if failure_patterns_to_avoid:
        lines.append(
            "- patterns to avoid (do NOT repeat these actions; "
            "they did not move any check):"
        )
        lines.extend(f"  - {line}" for line in failure_patterns_to_avoid)
    if relevant_evidence_summaries:
        lines.append(
            "- successful actions already on record "
            "(refer to these instead of re-running):"
        )
        lines.extend(f"  - {line}" for line in relevant_evidence_summaries)
    lines.extend(
        [
            "",
            "If a previous loop already created a file you need, prefer",
            "patch_file over a fresh write_file.",
            "If you already explored the workspace last loop, do NOT explore",
            "again — act on the patterns-to-avoid above.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
