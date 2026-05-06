"""Deterministic skill-card derivation (PRD §18.4).

Six pure-ish helpers that turn validated task state into the prose +
metadata fields of a :class:`SkillCardCandidate`. NO LLM calls — these
algorithms are byte-deterministic so two CI runs against the same
fixture produce identical output (PRD §18.5 golden test).

The only IO touchpoints are :func:`derive_tools` (reads
``list_successful_tool_call_evidence``) and :func:`derive_failures`
(reads ``list_events``); the rest are pure functions over the passed
``best_state`` / ``traces`` arguments.
"""
from __future__ import annotations

from hungerloop.models.blackboard import BestState
from hungerloop.models.tracing import LoopTrace
from hungerloop.repository.protocol import RepositoryProtocol


def derive_name(best_state: BestState) -> str:
    """First non-empty line of ``best_state.summary``, truncated to 80
    chars and stripped of trailing punctuation. Empty summary →
    ``'Skill from task <task_id>'``.
    """
    head = next(
        (line.strip() for line in best_state.summary.splitlines() if line.strip()),
        f"Skill from task {best_state.task_id}",
    )
    return head[:80].rstrip(".:;,!?")


def derive_steps(traces: list[LoopTrace]) -> list[str]:
    """One step per committed LoopTrace, in chronological order.

    Step text is ``delta_summary`` if non-empty, else
    ``f'Loop {loop_id}: committed candidate {candidate_state_id}'``.
    Rejected loops are skipped — we only persist the success path.
    """
    steps: list[str] = []
    for t in sorted(
        (t for t in traces if t.committed),
        key=lambda x: x.loop_id,
    ):
        text = (t.delta_summary or "").strip()
        if not text:
            text = (
                f"Loop {t.loop_id}: committed candidate "
                f"{t.best_state_id_after_loop or t.best_state_id_before_loop or 'unknown'}"
            )
        steps.append(text)
    return steps


def derive_tools(
    traces: list[LoopTrace], repo: RepositoryProtocol
) -> list[str]:
    """Unique tool names from ``tool_call`` evidence rows referenced
    by committed traces, sorted by first-seen loop_id then name.
    De-dupe is case-sensitive (matches what tool_harness registered).
    """
    if not traces:
        return []
    task_id = traces[0].task_id
    committed_loops = {t.loop_id for t in traces if t.committed}
    seen: dict[str, int] = {}
    for ev in repo.list_successful_tool_call_evidence(task_id):
        loop_id = ev.get("loop_id")
        if not isinstance(loop_id, int) or loop_id not in committed_loops:
            continue
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        name = payload.get("tool_name")
        if isinstance(name, str) and name not in seen:
            seen[name] = loop_id
    return sorted(seen.keys(), key=lambda n: (seen[n], n))


def derive_trigger_signals(
    best_state: BestState, _traces: list[LoopTrace]
) -> list[str]:
    """Accepted-check item ids prefixed with ``check:`` plus
    ``artifact:<id>`` entries for every artifact id on best.

    PRD §18.4 mandates de-dupe + stable sort. The traces argument is
    accepted for API symmetry with the other derivers; the v0.5e.1
    rule does not consult them but v0.6 may.
    """
    out: set[str] = set()
    for key in best_state.accepted_check_keys:
        item_id = key.split(":", 1)[0]
        out.add(f"check:{item_id}")
    for artifact_id in best_state.artifact_ids:
        out.add(f"artifact:{artifact_id}")
    return sorted(out)


def derive_preconditions(
    best_state: BestState, _traces: list[LoopTrace]
) -> list[str]:
    """Static rule set per PRD §18.4 (deliberately short; v0.6 may
    LLM-generate richer ones).
    """
    return [
        "workspace clean (no uncommitted candidates)",
        f"previously-passed checks: {len(best_state.accepted_check_keys)}",
    ]


def derive_failures(
    task_id: str, repo: RepositoryProtocol, *, cap: int = 20
) -> list[str]:
    """One entry per ``tool_call_failed`` and ``model_call_failed``
    event, capped at ``cap`` (newest kept).

    Format: ``"<event_type>: <error_type> (loop <loop_id>)"``.
    """
    rows = repo.list_events(
        task_id,
        event_types=["tool_call_failed", "model_call_failed"],
    )
    formatted: list[str] = []
    for ev in rows:
        event_type = ev.get("event_type")
        loop_id = ev.get("loop_id")
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        error_type = payload.get("error_type", "unknown")
        formatted.append(f"{event_type}: {error_type} (loop {loop_id})")
    return formatted[-cap:]
