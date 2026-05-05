"""``hungerloop new`` — create a fresh task."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import click
import yaml

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
    "--accept-file",
    "accept_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "YAML/JSON file with core_acceptance_checks and optional "
        "core_acceptance_mode."
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
    accept_file: Path | None,
    memory_consolidation: bool,
    task_id: str | None,
) -> None:
    """Compile a new HungerLedger from GOAL and acceptance check specs."""
    if not accept_specs and accept_file is None:
        raise click.UsageError(
            "At least one --accept JSON spec or --accept-file is required."
        )

    parsed_checks: list[dict[str, object]] = []
    core_acceptance_mode = "all"
    if accept_file is not None:
        file_checks, file_mode = _load_accept_file(accept_file)
        parsed_checks.extend(file_checks)
        core_acceptance_mode = file_mode

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
    ctx.repo.create_task(final_task_id, goal)

    compiler = RuleBasedCompiler()
    _, ledger = compiler.compile(
        final_task_id,
        goal,
        hints={
            "core_acceptance_checks": parsed_checks,
            "core_acceptance_mode": core_acceptance_mode,
            "enable_memory_consolidation": memory_consolidation,
        },
    )

    ctx.repo.set_hunger_policy(final_task_id, ctx.repo.get_hunger_policy(final_task_id))
    ctx.repo.save_hunger_ledger(final_task_id, ledger)
    for item in ledger.items:
        ctx.repo.save_hunger_item(item)

    click.echo(f"Created task: {final_task_id}")
    click.echo(f"  goal: {goal}")
    click.echo(f"  hunger items: {[item.id for item in ledger.items]}")


def _load_accept_file(path: Path) -> tuple[list[dict[str, object]], str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise click.UsageError(f"--accept-file must be valid YAML/JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise click.UsageError("--accept-file must contain a mapping.")
    checks = raw.get("core_acceptance_checks")
    if not isinstance(checks, list) or not checks:
        raise click.UsageError(
            "--accept-file requires non-empty core_acceptance_checks."
        )
    parsed: list[dict[str, object]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise click.UsageError(
                "core_acceptance_checks entries must be objects "
                f"(entry {index})."
            )
        parsed.append(check)
    mode = raw.get("core_acceptance_mode", "all")
    if mode not in ("all", "any"):
        raise click.UsageError("core_acceptance_mode must be 'all' or 'any'.")
    return parsed, str(mode)
