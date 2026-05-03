"""``hungerloop run`` — preflight, orchestrate, persist StopReport."""
from __future__ import annotations

import asyncio

import click

from hungerloop.cli.context import CliContext
from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.cli.preflight import PreflightError, check_resume_preflight
from hungerloop.services.skill_manager import SkillManager


@click.command("run")
@click.argument("task_id")
@click.option(
    "--max-loops",
    type=int,
    default=200,
    show_default=True,
    help="Defensive cap; HungerEngine normally terminates the loop earlier.",
)
@click.option(
    "--refill",
    "refill_loops",
    type=int,
    default=None,
    help=(
        "Credit N loop budgets before resuming; required when the previous "
        "stop_reason was HUNGER_EXPIRED."
    ),
)
@click.option(
    "--unblock-all",
    is_flag=True,
    default=False,
    help="Reset every BLOCKED hunger item to OPEN; required after BLOCKED stop.",
)
@click.option(
    "--resume",
    "resume_human",
    is_flag=True,
    default=False,
    help="Confirm that HUMAN_REQUIRED / HUMAN_PAUSED preconditions are resolved.",
)
@click.option(
    "--raise-cost-ceiling",
    "raise_cost_ceiling",
    type=float,
    default=None,
    help=(
        "New max_total_cost_usd; required when the previous stop_reason "
        "was SAFETY_STOP."
    ),
)
@click.pass_obj
def run(
    ctx: CliContext,
    task_id: str,
    max_loops: int,
    refill_loops: int | None,
    unblock_all: bool,
    resume_human: bool,
    raise_cost_ceiling: float | None,
) -> None:
    """Drive ``task_id`` through the orchestrator until a StopReport.

    Runs the preflight checks (PRD §18.3) before invoking the orchestrator,
    applies any requested refill / unblock / cost-ceiling raises *before*
    preflight, then persists the resulting StopReport per §28.16 / M4.
    """
    # Preflight runs *before* overrides so its comparisons (e.g.
    # raise_cost_ceiling vs. current ceiling) see the pre-resume policy.
    try:
        check_resume_preflight(
            ctx.repo,
            task_id,
            refill_loops=refill_loops,
            unblock_all=unblock_all,
            resume_human=resume_human,
            raise_cost_ceiling=raise_cost_ceiling,
        )
    except PreflightError as exc:
        click.echo(f"Preflight error: {exc}", err=True)
        raise click.exceptions.Exit(2) from exc

    _apply_user_overrides(
        ctx,
        task_id,
        refill_loops=refill_loops,
        unblock_all=unblock_all,
        raise_cost_ceiling=raise_cost_ceiling,
    )

    orchestrator = build_orchestrator(
        repo=ctx.repo,
        workspace_root=ctx.workspace_root,
        model_client=ctx.model_client,
        max_loops_safety_cap=max_loops,
    )
    orchestrator.workspace_manager.ensure_task_workspace(task_id)

    report = asyncio.run(orchestrator.run(task_id))
    skill_card = SkillManager(ctx.repo).maybe_create_skill_card(task_id, report)
    ctx.repo.save_stop_report(report)

    click.echo(f"Task {task_id} stopped: {report.stop_reason.value}")
    if skill_card is not None:
        click.echo(f"  skill_card: {skill_card.skill_id}")
    click.echo(f"  goal_status: {report.goal_status}")
    click.echo(f"  total_loops: {report.total_loops}")
    click.echo(f"  total_cost_usd: {report.total_cost_usd:.4f}")
    click.echo(f"  total_tokens: {report.total_tokens}")
    if report.recommendation:
        click.echo(f"  recommendation: {report.recommendation}")


def _apply_user_overrides(
    ctx: CliContext,
    task_id: str,
    *,
    refill_loops: int | None,
    unblock_all: bool,
    raise_cost_ceiling: float | None,
) -> None:
    """Translate CLI flags into repository state mutations.

    Done up-front so the same operation is observable both via
    ``hungerloop hunger ...`` and via the corresponding ``run`` flag.
    Side-effects:

    * ``--refill N`` → ``clock.loop_count = max(0, loop_count - N)`` and
      an audit event (PRD §28.12).
    * ``--unblock-all`` → every BLOCKED item flipped back to OPEN with
      counters reset, plus a per-item audit event (PRD §15.2).
    * ``--raise-cost-ceiling X`` → ``policy.max_total_cost_usd = X``.
    """
    if refill_loops is not None and refill_loops > 0:
        clock = ctx.repo.get_hunger_clock(task_id)
        before = clock.loop_count
        clock.loop_count = max(0, clock.loop_count - refill_loops)
        ctx.repo.save_hunger_clock(clock)
        ctx.repo.append_event(
            "hunger_refilled",
            {
                "amount_loops": refill_loops,
                "before": before,
                "after": clock.loop_count,
            },
            task_id=task_id,
        )

    if unblock_all:
        ledger = ctx.repo.get_hunger_ledger(task_id)
        from hungerloop.models.enums import HungerItemStatus

        for item in ledger.items:
            if item.status == HungerItemStatus.BLOCKED:
                item.status = HungerItemStatus.OPEN
                item.consecutive_failure_count = 0
                item.last_progress_loop_id = None
                ctx.repo.save_hunger_item(item)
                ctx.repo.append_event(
                    "human_unblocked_hunger_item",
                    {"item_id": item.id, "via": "--unblock-all"},
                    task_id=task_id,
                )
        ctx.repo.reset_no_progress_streak(task_id)

    if raise_cost_ceiling is not None:
        policy = ctx.repo.get_hunger_policy(task_id)
        policy.max_total_cost_usd = raise_cost_ceiling
        # InMemoryRepository's set_hunger_policy is the helper; protocol-only
        # callers will reach a SQLiteRepository setter when v0.5b ships.
        if hasattr(ctx.repo, "set_hunger_policy"):
            ctx.repo.set_hunger_policy(task_id, policy)
        ctx.repo.append_event(
            "cost_ceiling_raised",
            {"new_ceiling_usd": raise_cost_ceiling},
            task_id=task_id,
        )
