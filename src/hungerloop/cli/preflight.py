"""Resume preflight logic (PRD §18.3).

The CLI must refuse to invoke :class:`LoopOrchestrator` when the previous
run ended in a state that requires *user* action before continuing:

* ``HUNGER_EXPIRED`` — the user must refill loops (or pass ``--refill``).
* ``BLOCKED`` — the user must unblock at least one item (or pass
  ``--unblock-all``).
* ``HUMAN_REQUIRED`` — the user must resolve whatever input the run was
  waiting on; CLI accepts ``--resume`` once they confirm.
* ``SAFETY_STOP`` — the cost ceiling was hit; user must raise it
  (``--raise-cost-ceiling``).

:class:`PreflightError` carries an *actionable* message so the CLI can
print it verbatim and exit; the orchestrator is never called.

This module is import-light by design (no click, no orchestrator deps)
so it can be unit-tested without spinning up the whole CLI.
"""
from __future__ import annotations

from hungerloop.models.enums import StopReason
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

    Raises:
        PreflightError: When the precondition for the prior stop reason
            is not satisfied. Caller should print ``str(exc)`` and exit
            without invoking the Orchestrator.
    """
    last = repo.get_last_stop_reason(task_id)
    if last is None:
        return  # fresh task — nothing to preflight

    if last == StopReason.HUNGER_EXPIRED:
        if refill_loops is None or refill_loops <= 0:
            raise PreflightError(
                f"Task {task_id} previously stopped with HUNGER_EXPIRED. "
                "Pass --refill <loops> (or run "
                f"'hungerloop hunger refill {task_id} --loops <N>') first."
            )

    elif last == StopReason.BLOCKED:
        if not unblock_all and not _has_open_items(repo, task_id):
            raise PreflightError(
                f"Task {task_id} previously stopped with BLOCKED. "
                "Pass --unblock-all (or unblock individual items via "
                f"'hungerloop hunger unblock {task_id} <H-XXX>') first."
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

    elif last == StopReason.HUMAN_PAUSED:
        if not resume_human:
            raise PreflightError(
                f"Task {task_id} previously stopped with HUMAN_PAUSED. "
                "Run 'hungerloop hunger resume "
                f"{task_id}' to unfreeze, then re-run."
            )

    # DONE / ERROR fall through: re-running is allowed; orchestrator will
    # surface DONE again immediately or proceed if state has been mutated.


def _has_open_items(repo: RepositoryProtocol, task_id: str) -> bool:
    """True iff at least one item in the ledger is currently active."""
    ledger = repo.get_hunger_ledger(task_id)
    return ledger.has_active_items()
