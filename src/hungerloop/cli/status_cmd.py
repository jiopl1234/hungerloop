"""``hungerloop status`` — print task summary (PRD §18.4)."""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext
from hungerloop.cli.status_format import format_status


@click.command("status")
@click.argument("task_id")
@click.pass_obj
def status(ctx: CliContext, task_id: str) -> None:
    """Print stop_reason / loops / usage / hunger items for ``task_id``."""
    click.echo(format_status(ctx.repo, task_id))
