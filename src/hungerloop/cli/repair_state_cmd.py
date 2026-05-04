"""``hungerloop repair-state`` command (PRD §16.3).

Two modes, mutually exclusive:

* ``--check`` — read-only; prints one line per divergence.
* ``--fix`` — repairs the limited v0.5b.0 set (D4/D5); refuses corruption
  (D2/D3), stale locks (D6, points at ``--steal-lock``), and dangling
  validation rows (D7, deferred to v0.5b.1).

Exit codes are deliberate so CI pipelines can react to them:

* ``0`` — clean (``--check``) or all repairable items fixed (``--fix``).
* ``1`` — warnings present (``--check`` only).
* ``2`` — corruption detected (``--check``) or refused during fix.
* ``3`` — ``--fix`` with no actionable divergences (``--fix`` only).
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
    "mode_fix",
    is_flag=True,
    help="Apply the limited v0.5b.0 fix set (D4/D5).",
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

    # --fix path
    actionable = [
        d for d in divergences if not d.corruption and d.kind in {"D4", "D5"}
    ]
    if not divergences:
        click.echo("clean")
        return  # exit 0
    if any(d.corruption for d in divergences):
        for d in divergences:
            click.echo(_format_line(d))
        click.echo(
            "refusing to fix: corruption detected (D2/D3); restore from backup",
            err=True,
        )
        click.get_current_context().exit(2)
        return
    if not actionable:
        for d in divergences:
            click.echo(_format_line(d))
        click.echo(
            "no auto-fixable divergences in this release",
            err=True,
        )
        click.get_current_context().exit(3)
        return

    fixed_count = 0
    for divergence in divergences:
        outcome = service.apply_fix(divergence)
        prefix = "fixed " if outcome.fixed else "skip  "
        click.echo(f"{prefix}{divergence.kind} {divergence.target} — {outcome.summary}")
        if outcome.fixed:
            fixed_count += 1
    click.echo(f"summary: {fixed_count} fixed of {len(divergences)} divergences")


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
