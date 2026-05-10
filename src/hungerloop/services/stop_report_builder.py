"""Construct a :class:`StopReport` with the §28.6 / M20 mapping.

The Orchestrator emits a stop report at every terminal transition. The
mapping ``StopReason → GoalStatus`` is fixed in PRD §28.6:

| stop_reason     | goal_status                                                    |
|-----------------|----------------------------------------------------------------|
| DONE            | completed                                                      |
| HUNGER_EXPIRED  | partial (best non-empty + accepted_check_keys) else abandoned  |
| BUDGET_EXHAUSTED| completed when tier 0 is done; else partial/abandoned by best  |
| BLOCKED         | blocked                                                        |
| HUMAN_REQUIRED  | paused                                                         |
| HUMAN_PAUSED    | paused                                                         |
| SAFETY_STOP     | abandoned                                                      |
| ERROR           | abandoned                                                      |

This module is the *single* place that knows the mapping; orchestrator
code calls :func:`build_stop_report` and never constructs a
:class:`StopReport` directly.
"""
from __future__ import annotations

from hungerloop.models.enums import StopReason
from hungerloop.models.tracing import GoalStatus, StopReport
from hungerloop.repository.protocol import RepositoryProtocol


def _goal_status_for(
    stop_reason: StopReason,
    *,
    has_useful_best: bool,
    tier0_done: bool,
) -> GoalStatus:
    """Apply the §28.6 mapping table.

    Args:
        stop_reason: The terminal :class:`StopReason`.
        has_useful_best: True iff a :class:`BestState` exists with at least
            one accepted check; the only reason this affects the mapping
            is the ``HUNGER_EXPIRED`` partial-vs-abandoned split.
    """
    if stop_reason == StopReason.DONE:
        return "completed"
    if stop_reason == StopReason.HUNGER_EXPIRED:
        return "partial" if has_useful_best else "abandoned"
    if stop_reason == StopReason.BUDGET_EXHAUSTED:
        if tier0_done:
            return "completed"
        return "partial" if has_useful_best else "abandoned"
    if stop_reason == StopReason.BLOCKED:
        return "blocked"
    if stop_reason in (StopReason.HUMAN_REQUIRED, StopReason.HUMAN_PAUSED):
        return "paused"
    # SAFETY_STOP, ERROR
    return "abandoned"


def build_stop_report(
    repo: RepositoryProtocol,
    task_id: str,
    stop_reason: StopReason,
    *,
    summary: str = "",
    recommendation: str = "",
) -> StopReport:
    """Assemble a :class:`StopReport` from current repository state.

    Pulls best-state metadata, ledger residuals, and cumulative usage
    counters so callers don't have to. ``summary`` and ``recommendation``
    are pass-throughs for human-facing context (e.g. CLI messages).
    """
    best = repo.get_best_state(task_id)
    accepted_check_keys = best.accepted_check_keys if best else []
    has_useful_best = bool(best and accepted_check_keys)

    ledger = repo.get_hunger_ledger(task_id)
    tier0_done = ledger.tier_is_done(0)
    remaining = [item.id for item in ledger.unfinished_items()]
    blocked = [item.id for item in ledger.blocked_items()]

    clock = repo.get_hunger_clock(task_id)
    usage = repo.get_usage_snapshot(task_id)

    return StopReport(
        task_id=task_id,
        stop_reason=stop_reason,
        goal_status=_goal_status_for(
            stop_reason,
            has_useful_best=has_useful_best,
            tier0_done=tier0_done,
        ),
        final_best_state_id=best.state_id if best else None,
        best_state_summary=best.summary if best else None,
        accepted_check_keys_count=len(accepted_check_keys),
        total_loops=clock.loop_count,
        total_cost_usd=usage.cost_usd,
        total_tokens=usage.tokens,
        remaining_hunger_items=remaining,
        blocked_hunger_items=blocked,
        recommendation=recommendation,
        summary=summary,
    )
