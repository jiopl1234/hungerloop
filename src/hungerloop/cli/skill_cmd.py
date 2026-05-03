"""``hungerloop skill`` — inspect SkillCard rows (PRD §20 / §22.3)."""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext


@click.group("skill")
def skill() -> None:
    """Skill card inspection."""


@skill.command("list")
@click.argument("task_id", required=False)
@click.pass_obj
def skill_list(ctx: CliContext, task_id: str | None) -> None:
    """Print SkillCard summaries; filters by ``task_id`` when given."""
    cards = ctx.repo.list_skill_cards(task_id)
    if not cards:
        scope = task_id if task_id is not None else "any task"
        click.echo(f"No skill cards for {scope}.")
        return
    for card in cards:
        click.echo(
            f"{card.skill_id} task={card.task_id} "
            f"checks={len(card.accepted_check_keys)} "
            f"name={card.name}"
        )
