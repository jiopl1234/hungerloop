"""``hungerloop repair-state`` command (PRD §13 / §16.3).

Two modes, mutually exclusive:

* ``--check`` — read-only; prints one line per divergence.
* ``--fix`` (alias ``--apply``) — repairs the auto-fixable set
  (D4/D5/D10/D11) and refuses the rest (corruption: D2/D3/D8/D9;
  warning-only: D6/D7/D12/D13).

Exit codes are deliberate so CI pipelines can react to them:

* ``0`` — clean (``--check``) or every divergence resolved
  successfully (``--fix``).
* ``1`` — warnings present (``--check`` only).
* ``2`` — corruption detected (``--check``) or any corruption
  divergence refused during ``--fix``.
* ``3`` — ``--fix`` with no actionable divergences.
"""
from __future__ import annotations

import click

from hungerloop.cli.context import CliContext
from hungerloop.services.repair_state import (
    DEFAULT_STALE_THRESHOLD_SECONDS,
    Divergence,
    RepairStateService,
)


@click.command("repair-state")
@click.argument("task_id")
@click.option(
    "--check",
    "mode_check",
    is_flag=True,
    help="Read-only divergence detection; no filesystem mutations.",
)
@click.option(
    "--fix",
    "--apply",
    "mode_fix",
    is_flag=True,
    help="Apply the auto-fixable set (D4/D5/D10/D11).",
)
@click.option(
    "--lock-stale-sec",
    type=int,
    default=DEFAULT_STALE_THRESHOLD_SECONDS,
    show_default=True,
    help="Threshold for treating a task lock as stale (D6).",
)
@click.pass_obj
def repair_state(
    ctx: CliContext,
    task_id: str,
    mode_check: bool,
    mode_fix: bool,
    lock_stale_sec: int,
) -> None:
    """Detect (and optionally repair) workspace ↔ repository divergence."""
    if mode_check == mode_fix:
        raise click.UsageError(
            "Pass exactly one of --check or --fix."
        )

    service = RepairStateService(
        repo=ctx.repo,
        workspace_root=ctx.workspace_root,
        stale_threshold_seconds=lock_stale_sec,
    )
    divergences = service.detect(task_id)

    if mode_check:
        _print_check_output(divergences)
        ctx_obj = click.get_current_context()
        ctx_obj.exit(_check_exit_code(divergences))
        return

    # --fix path (FR-12 / FR-13: dispatch every row, refuse where the
    # service refuses, exit 2 only if any corruption refusal occurred).
    if not divergences:
        click.echo("clean")
        return  # exit 0

    fixable_kinds = {"D4", "D5", "D10", "D11"}
    has_fixable = any(d.kind in fixable_kinds for d in divergences)
    has_corruption = any(d.corruption for d in divergences)

    if not has_fixable:
        for d in divergences:
            click.echo(_format_line(d))
        if has_corruption:
            click.echo(
                "refusing to fix: corruption detected; restore from backup",
                err=True,
            )
            click.get_current_context().exit(2)
            return
        click.echo(
            "no auto-fixable divergences in this release",
            err=True,
        )
        click.get_current_context().exit(3)
        return

    fixed_count = 0
    refused_corruption = False
    for divergence in divergences:
        outcome = service.apply_fix(divergence)
        prefix = "fixed " if outcome.fixed else "skip  "
        click.echo(f"{prefix}{divergence.kind} {divergence.target} — {outcome.summary}")
        if outcome.fixed:
            fixed_count += 1
        elif divergence.corruption:
            refused_corruption = True
    click.echo(f"summary: {fixed_count} fixed of {len(divergences)} divergences")
    if refused_corruption:
        click.get_current_context().exit(2)


def _print_check_output(divergences: list[Divergence]) -> None:
    if not divergences:
        click.echo("clean")
        return
    for d in divergences:
        click.echo(_format_line(d))


def _format_line(d: Divergence) -> str:
    severity = "CORRUPT" if d.corruption else "WARN   "
    return f"{severity} {d.kind} {d.target} — {d.detail}"


def _check_exit_code(divergences: list[Divergence]) -> int:
    if not divergences:
        return 0
    if any(d.corruption for d in divergences):
        return 2
    return 1
