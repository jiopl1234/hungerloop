"""Resume preflight logic (PRD §13 / §18.3).

The CLI must refuse to invoke :class:`LoopOrchestrator` when the previous
run ended in a state that requires *user* action before continuing:

* ``DONE`` — the task already finished; require ``--reset`` to wipe and
  start over.
* ``HUNGER_EXPIRED`` — the user must refill loops (or pass ``--refill``).
* ``BUDGET_EXHAUSTED`` — the user must refill loops or reset the task.
* ``BLOCKED`` — the user must unblock at least one item (or pass
  ``--unblock-all``).
* ``HUMAN_REQUIRED`` — the user must resolve whatever input the run was
  waiting on; CLI accepts ``--resume`` once they confirm.
* ``HUMAN_PAUSED`` — same as HUMAN_REQUIRED for the resume gate.
* ``SAFETY_STOP`` — the cost ceiling was hit; user must raise it
  (``--raise-cost-ceiling``) and pass ``--resume``.
* ``ERROR`` — must run ``hungerloop repair-state`` first; the gate
  passes only when a ``repair_state_action`` event exists with
  ``created_at > stop_report.created_at`` (or ``--skip-repair-check``).

:class:`PreflightError` carries an *actionable* message so the CLI can
print it verbatim and exit; the orchestrator is never called.

This module is import-light by design (no click, no orchestrator deps)
so it can be unit-tested without spinning up the whole CLI.
"""
from __future__ import annotations

from hungerloop.models.enums import StopReason
from hungerloop.models.events import EventType
from hungerloop.repository.protocol import RepositoryProtocol


class PreflightError(RuntimeError):
    """Raised when the CLI refuses to call the orchestrator.

    The message is intended for direct display to the user — keep it
    actionable (what to do next), not just descriptive.
    """


def check_resume_preflight(
    repo: RepositoryProtocol,
    task_id: str,
    *,
    refill_loops: int | None = None,
    unblock_all: bool = False,
    resume_human: bool = False,
    raise_cost_ceiling: float | None = None,
    reset: bool = False,
    skip_repair_check: bool = False,
) -> None:
    """Inspect ``last_stop_reason`` and reject if user action is missing.

    Args:
        repo: Repository protocol instance.
        task_id: Task identifier to check.
        refill_loops: How many loop budgets to credit before resuming.
            Required when the previous stop was ``HUNGER_EXPIRED``.
        unblock_all: True if the caller invoked ``--unblock-all`` (or one
            or more individual unblocks happened before this call).
            Required when the previous stop was ``BLOCKED``.
        resume_human: True if the caller confirmed they resolved the
            human-required input (``--resume``).
        raise_cost_ceiling: New ``max_total_cost_usd`` ceiling. Required
            when the previous stop was ``SAFETY_STOP``.
        reset: True if the caller invoked ``--reset``. Required when the
            previous stop was ``DONE``.
        skip_repair_check: True if the caller invoked
            ``--skip-repair-check`` to bypass the ERROR-recovery gate.

    Raises:
        PreflightError: When the precondition for the prior stop reason
            is not satisfied. Caller should print ``str(exc)`` and exit
            without invoking the Orchestrator.
    """
    last = repo.get_last_stop_reason(task_id)
    if last is None:
        return  # fresh task — nothing to preflight

    if last == StopReason.DONE:
        if not reset:
            raise PreflightError(
                f"Task {task_id} already completed (DONE). "
                "Pass --reset to wipe state and start a new run."
            )

    elif last == StopReason.HUNGER_EXPIRED:
        if refill_loops is None or refill_loops <= 0:
            raise PreflightError(
                f"Task {task_id} previously stopped with HUNGER_EXPIRED. "
                "Pass --refill <loops> (or run "
                f"'hungerloop hunger refill {task_id} --loops <N>') first."
            )

    elif last == StopReason.BUDGET_EXHAUSTED:
        if reset:
            return
        if refill_loops is None or refill_loops <= 0:
            raise PreflightError(
                f"Task {task_id} previously stopped with BUDGET_EXHAUSTED. "
                "Pass --refill <loops> to continue refinement, or --reset "
                "to start a new run."
            )

    elif last == StopReason.BLOCKED:
        if not unblock_all and not _has_open_items(repo, task_id):
            raise PreflightError(
                f"Task {task_id} previously stopped with BLOCKED. "
                "Pass --unblock-all (or unblock individual items via "
                f"'hungerloop hunger unblock {task_id} <H-XXX>') first."
            )
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with BLOCKED. "
                "After unblocking, pass --resume to continue."
            )

    elif last == StopReason.HUMAN_REQUIRED:
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with HUMAN_REQUIRED. "
                "Resolve the missing auth/approval/input, then pass "
                "--resume to continue."
            )

    elif last == StopReason.SAFETY_STOP:
        if raise_cost_ceiling is None:
            raise PreflightError(
                f"Task {task_id} previously stopped with SAFETY_STOP. "
                "Pass --raise-cost-ceiling <USD> to continue."
            )
        policy = repo.get_hunger_policy(task_id)
        if raise_cost_ceiling <= policy.max_total_cost_usd:
            raise PreflightError(
                f"--raise-cost-ceiling must exceed the current ceiling "
                f"(${policy.max_total_cost_usd:.4f})."
            )
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with SAFETY_STOP. "
                "After --raise-cost-ceiling, pass --resume to continue."
            )

    elif last == StopReason.HUMAN_PAUSED:
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with HUMAN_PAUSED. "
                "Pass --resume to unfreeze during this run, or run "
                f"'hungerloop hunger resume {task_id}' first."
            )

    elif last == StopReason.ERROR:
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with ERROR. "
                "Pass --resume after running 'hungerloop repair-state' "
                f"{task_id}."
            )
        if not skip_repair_check and not _has_recent_repair_event(repo, task_id):
            raise PreflightError(
                f"Cannot resume task {task_id}: last stop reason is ERROR, "
                "and no repair-state run has been recorded since.\n"
                f"Run:\n  hungerloop repair-state {task_id}\n"
                f"then:\n  hungerloop run {task_id} --resume\n\n"
                "If repair-state surfaced no fixable divergences, pass "
                "--resume --skip-repair-check to override "
                "(audit-logged via repair_state_action with action=\"skipped\")."
            )


def _has_open_items(repo: RepositoryProtocol, task_id: str) -> bool:
    """True iff at least one item in the ledger is currently active."""
    ledger = repo.get_hunger_ledger(task_id)
    return ledger.has_active_items()


def _has_recent_repair_event(repo: RepositoryProtocol, task_id: str) -> bool:
    """True iff a ``repair_state_action`` event was appended *after* the
    most-recent ``stop_report_created`` event (PRD §13.1).

    Uses the per-task event insertion order as the comparator. We
    intentionally do not compare ``created_at`` strings — both backends
    use second-resolution timestamps, so two events appended in the
    same wall-clock second would tie and the gate would behave
    nondeterministically. Listing the task's full event log once and
    checking that some ``repair_state_action`` row appears after the
    latest ``stop_report_created`` row is monotonic and tie-free.
    """
    rows = repo.list_events(
        task_id,
        event_types=[
            EventType.STOP_REPORT_CREATED.value,
            EventType.REPAIR_STATE_ACTION.value,
        ],
    )
    last_stop_index = -1
    for i, ev in enumerate(rows):
        if ev.get("event_type") == EventType.STOP_REPORT_CREATED.value:
            last_stop_index = i
    if last_stop_index < 0:
        return False
    return any(
        ev.get("event_type") == EventType.REPAIR_STATE_ACTION.value
        for ev in rows[last_stop_index + 1 :]
    )
