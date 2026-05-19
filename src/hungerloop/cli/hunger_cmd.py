"""``hungerloop hunger`` operations group (PRD §18.5).

Five subcommands map directly onto repository mutations + audit events:

* ``refill --loops N`` — credit N loop budgets back to the clock
  (PRD §28.12 / M13).
* ``unblock <item_id>`` — flip a single BLOCKED item to OPEN
  (PRD §15.2).
* ``unblock-all`` — flip every BLOCKED item to OPEN.
* ``freeze`` — set ``clock.frozen = True`` so the next loop emits
  ``HUMAN_PAUSED``.
* ``resume`` — clear ``clock.frozen``. Note: the CLI ``--resume`` flag on
  ``hungerloop run`` is *separate* — it confirms the user resolved a
  ``HUMAN_REQUIRED`` precondition.
"""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext
from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.events import EventType


@click.group("hunger")
def hunger() -> None:
    """Hunger-state operations (refill, unblock, freeze, resume)."""


@hunger.command("refill")
@click.argument("task_id")
@click.option(
    "--loops",
    "loops",
    type=int,
    required=True,
    help="Number of loop budgets to credit; subtracted from clock.loop_count.",
)
@click.pass_obj
def hunger_refill(ctx: CliContext, task_id: str, loops: int) -> None:
    """Credit ``loops`` loop budgets back to the clock (§28.12 / M13)."""
    if loops <= 0:
        raise click.UsageError("--loops must be a positive integer.")
    clock = ctx.repo.get_hunger_clock(task_id)
    before = clock.loop_count
    clock.loop_count = max(0, clock.loop_count - loops)
    ctx.repo.save_hunger_clock(clock)
    ctx.repo.append_event(
        EventType.HUNGER_REFILLED,
        {"amount_loops": loops, "before": before, "after": clock.loop_count},
        task_id=task_id,
    )
    click.echo(
        f"refilled {loops} loops on {task_id}: "
        f"loop_count {before} -> {clock.loop_count}"
    )


@hunger.command("unblock")
@click.argument("task_id")
@click.argument("item_id")
@click.pass_obj
def hunger_unblock(ctx: CliContext, task_id: str, item_id: str) -> None:
    """Flip a single BLOCKED item to OPEN (PRD §15.2).

    Idempotent: re-running on an already-OPEN item exits 0 with a
    "no-op" message and emits no event so audit logs stay clean.
    """
    ledger = ctx.repo.get_hunger_ledger(task_id)
    item = next((candidate for candidate in ledger.items if candidate.id == item_id), None)
    if item is None:
        raise click.UsageError(f"hunger item not found: {item_id}")
    if item.status != HungerItemStatus.BLOCKED:
        click.echo(
            f"item {item_id} is already {item.status.value} — no-op (idempotent)."
        )
        return
    item.status = HungerItemStatus.OPEN
    item.consecutive_failure_count = 0
    item.last_progress_loop_id = None
    ctx.repo.save_hunger_ledger(task_id, ledger)
    ctx.repo.append_event(
        EventType.HUMAN_UNBLOCKED_HUNGER_ITEM,
        {"item_id": item_id, "via": "hunger unblock"},
        task_id=task_id,
    )
    click.echo(f"unblocked {item_id} on {task_id}")


@hunger.command("unblock-all")
@click.argument("task_id")
@click.pass_obj
def hunger_unblock_all(ctx: CliContext, task_id: str) -> None:
    """Flip every BLOCKED item to OPEN (PRD §15.2).

    Idempotent: when no items are BLOCKED, exits 0 with a "no-op"
    message and emits no per-item events. The streak reset still
    runs so a clean ledger doesn't leave a stale streak counter.
    """
    ledger = ctx.repo.get_hunger_ledger(task_id)
    flipped: list[str] = []
    for item in ledger.items:
        if item.status == HungerItemStatus.BLOCKED:
            item.status = HungerItemStatus.OPEN
            item.consecutive_failure_count = 0
            item.last_progress_loop_id = None
            ctx.repo.append_event(
                EventType.HUMAN_UNBLOCKED_HUNGER_ITEM,
                {"item_id": item.id, "via": "hunger unblock-all"},
                task_id=task_id,
            )
            flipped.append(item.id)
    if flipped:
        ctx.repo.save_hunger_ledger(task_id, ledger)
    ctx.repo.reset_no_progress_streak(task_id)
    if not flipped:
        click.echo(
            f"no BLOCKED items on {task_id} — no-op (idempotent)."
        )
        return
    click.echo(f"unblocked {len(flipped)} items on {task_id}: {flipped}")


@hunger.command("freeze")
@click.argument("task_id")
@click.pass_obj
def hunger_freeze(ctx: CliContext, task_id: str) -> None:
    """Set ``clock.frozen = True``; next ``run`` emits HUMAN_PAUSED."""
    clock = ctx.repo.get_hunger_clock(task_id)
    clock.frozen = True
    ctx.repo.save_hunger_clock(clock)
    ctx.repo.append_event(EventType.HUNGER_FROZEN, {}, task_id=task_id)
    click.echo(f"froze hunger clock on {task_id}")


@hunger.command("resume")
@click.argument("task_id")
@click.pass_obj
def hunger_resume(ctx: CliContext, task_id: str) -> None:
    """Clear ``clock.frozen`` (NB: not the same as ``run --resume``)."""
    clock = ctx.repo.get_hunger_clock(task_id)
    clock.frozen = False
    ctx.repo.save_hunger_clock(clock)
    ctx.repo.append_event(EventType.HUNGER_RESUMED, {}, task_id=task_id)
    click.echo(f"resumed hunger clock on {task_id}")
