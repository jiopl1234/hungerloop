"""``hungerloop new`` — create a fresh task."""
from __future__ import annotations

import json
import uuid

import click

from hungerloop.cli.context import CliContext
from hungerloop.services.requirement_compiler import RuleBasedCompiler


@click.command("new")
@click.argument("goal", type=str)
@click.option(
    "--accept",
    "accept_specs",
    multiple=True,
    help=(
        "JSON-encoded AcceptanceCheck spec; repeat for multiple. "
        'Example: \'{"check_type":"file_exists","params":{"path":"report.md"}}\''
    ),
)
@click.option(
    "--memory-consolidation",
    is_flag=True,
    default=False,
    help="Add an H-003 memory_consolidation hunger item.",
)
@click.option(
    "--task-id",
    "task_id",
    default=None,
    help="Override the auto-generated task_id.",
)
@click.pass_obj
def new(
    ctx: CliContext,
    goal: str,
    accept_specs: tuple[str, ...],
    memory_consolidation: bool,
    task_id: str | None,
) -> None:
    """Compile a new HungerLedger from GOAL and acceptance check specs."""
    if not accept_specs:
        raise click.UsageError(
            "At least one --accept JSON spec is required."
        )

    parsed_checks: list[dict[str, object]] = []
    for raw in accept_specs:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.UsageError(
                f"--accept must be valid JSON; got: {raw}"
            ) from exc
        if not isinstance(parsed, dict):
            raise click.UsageError(
                f"--accept JSON must be an object, got {type(parsed).__name__}"
            )
        parsed_checks.append(parsed)

    final_task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"

    compiler = RuleBasedCompiler()
    _, ledger = compiler.compile(
        final_task_id,
        goal,
        hints={
            "core_acceptance_checks": parsed_checks,
            "enable_memory_consolidation": memory_consolidation,
        },
    )

    ctx.repo.save_hunger_ledger(final_task_id, ledger)
    for item in ledger.items:
        ctx.repo.save_hunger_item(item)

    click.echo(f"Created task: {final_task_id}")
    click.echo(f"  goal: {goal}")
    click.echo(f"  hunger items: {[item.id for item in ledger.items]}")
