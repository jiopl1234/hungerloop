"""``hungerloop memory`` — inspect MemoryCandidate rows (PRD §19 / §22.3)."""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext


@click.group("memory")
def memory() -> None:
    """Memory candidate inspection."""


@memory.command("list")
@click.argument("task_id")
@click.pass_obj
def memory_list(ctx: CliContext, task_id: str) -> None:
    """Print each :class:`MemoryCandidate` for ``task_id`` with predicate flags."""
    candidates = ctx.repo.list_memory_candidates(task_id)
    if not candidates:
        click.echo(f"No memory candidates for {task_id}.")
        return
    for c in candidates:
        flags = "".join(
            "1" if getattr(c, name) else "0"
            for name in ("action_verified", "reusable", "non_volatile", "traceable")
        )
        click.echo(
            f"{c.candidate_id} [{flags}] {c.memory_type}: {c.content}"
        )
