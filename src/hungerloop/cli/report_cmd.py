"""``hungerloop report`` — machine-first task summary (PRD §18.3).

JSON v1 by default for downstream tools (CI, dashboards, PR commenters);
``--format markdown`` for chat / PR comments. Two formats evolve
independently per decision §11.1.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import click

from hungerloop.cli.context import CliContext
from hungerloop.cli.report_format import build_report_dict, format_markdown


@click.command("report")
@click.argument("task_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default="json",
    show_default=True,
    help="Output format. JSON is the wire contract; markdown is for humans.",
)
@click.pass_obj
def report(ctx: CliContext, task_id: str, fmt: str) -> None:
    """Print a stable machine-readable summary for ``task_id``.

    Exit codes (PRD §18.3):
        0 — task found, report emitted (regardless of stop_reason).
        1 — task not found; stderr message; stdout empty.
    """
    if not _task_exists(ctx, task_id):
        click.echo(f"Task not found: {task_id}", err=True)
        raise click.exceptions.Exit(1)

    report_dict = build_report_dict(ctx.repo, task_id)
    if fmt == "json":
        # ensure_ascii=False keeps non-ASCII goals (Chinese, etc.) readable;
        # sort_keys=False preserves the schema's intentional field order.
        click.echo(json.dumps(report_dict, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(format_markdown(report_dict))


def _task_exists(ctx: CliContext, task_id: str) -> bool:
    """A task is considered initialized if any of its persistent state is
    populated — policy, ledger, traces, or stop reports.

    SQLiteRepository will replace this with ``repo.get_task(task_id) is
    not None`` once TaskRecord lands (PRD §3.4); the helper keeps v0.5a
    InMemory tests green in the meantime.
    """
    repo: Any = ctx.repo
    if hasattr(repo, "get_task"):
        record = repo.get_task(task_id)
        if record is not None:
            return True

    policies = getattr(repo, "_policies", {}) or {}
    if task_id in policies:
        return True
    ledgers = getattr(repo, "_ledgers", {}) or {}
    if task_id in ledgers:
        return True
    history = getattr(repo, "_stop_reports_history", {}) or {}
    if task_id in history:
        return True
    loop_traces = getattr(repo, "_loop_traces", {}) or {}
    return any(tid == task_id for (tid, _loop_id) in loop_traces.keys())
