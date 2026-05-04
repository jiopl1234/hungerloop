"""``hungerloop memory`` — inspect MemoryCandidate rows (PRD §19 / §22.3)."""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext

_STATES = ("proposed", "approved", "rejected", "expired", "superseded")


@click.group("memory")
def memory() -> None:
    """Memory candidate inspection."""


@memory.command("list")
@click.argument("task_id")
@click.option(
    "--state",
    type=click.Choice([*_STATES, "all"]),
    default="all",
    show_default=True,
    help=(
        "Filter by lifecycle state (PRD §19.1). v0.5c only emits "
        "'proposed'; the other values are reserved for v0.6 promotion."
    ),
)
@click.pass_obj
def memory_list(ctx: CliContext, task_id: str, state: str) -> None:
    """Print each :class:`MemoryCandidate` for ``task_id`` with predicate flags."""
    candidates = ctx.repo.list_memory_candidates(task_id)
    if state != "all":
        # Filter in Python — InMemoryRepository has no indexed query, and
        # SQLiteRepository uses ``idx_memory_state`` once it lands. v0.5c
        # never has enough rows for the linear scan to matter.
        candidates = [c for c in candidates if c.state == state]
    if not candidates:
        click.echo(f"No memory candidates for {task_id}.")
        return
    for c in candidates:
        flags = "".join(
            "1" if getattr(c, name) else "0"
            for name in ("action_verified", "reusable", "non_volatile", "traceable")
        )
        click.echo(
            f"{c.candidate_id} [{c.state}] [{flags}] {c.memory_type}: {c.content}"
        )
