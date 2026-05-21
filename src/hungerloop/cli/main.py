"""HungerLoop CLI entry point.

v0.5a CLI surface (PRD §18):

* ``hungerloop new`` — compile a task from goal + acceptance specs.
* ``hungerloop run`` — preflight then drive the orchestrator.
* ``hungerloop status`` — print task summary.
* ``hungerloop hunger {refill,unblock,unblock-all,freeze,resume}`` —
  human operations on hunger state.
* ``hungerloop workspace`` / ``hungerloop checks`` — pre-existing
  inspection helpers (v0.4.1).

Production invocations create/open ``hungerloop.sqlite`` in the current
workspace. Tests can still inject a :class:`CliContext` carrying any
``RepositoryProtocol`` implementation.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from hungerloop.cli.checks_cmd import checks
from hungerloop.cli.context import CliContext
from hungerloop.cli.hunger_cmd import hunger
from hungerloop.cli.memory_cmd import memory
from hungerloop.cli.mission_cmd import mission
from hungerloop.cli.new_cmd import new
from hungerloop.cli.repair_state_cmd import repair_state
from hungerloop.cli.report_cmd import report
from hungerloop.cli.run_cmd import run
from hungerloop.cli.skill_cmd import skill
from hungerloop.cli.status_cmd import status
from hungerloop.cli.trace_cmd import trace
from hungerloop.cli.workspace_cmd import workspace
from hungerloop.repository.migration_errors import (
    DownMigrationDisallowed,
    MigrationFailedError,
    SchemaTooNewError,
)
from hungerloop.repository.sqlite_migrator import ReadOnlyDbOutdatedError
from hungerloop.repository.sqlite_repo import SQLiteRepository

try:
    _PACKAGE_VERSION = version("hungerloop")
except PackageNotFoundError:
    # Editable install / source checkout without metadata: fall back gracefully.
    _PACKAGE_VERSION = "0+source"


def _default_context() -> CliContext:
    """Build the default :class:`CliContext` for production use.

    The workspace is the invoking process' current directory. State is
    persisted in ``hungerloop.sqlite`` next to the workspace directories.
    """
    workspace_root = Path.cwd()
    try:
        repo = SQLiteRepository.open(workspace_root / "hungerloop.sqlite")
    except ReadOnlyDbOutdatedError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(4) from exc
    except (
        DownMigrationDisallowed,
        MigrationFailedError,
        SchemaTooNewError,
    ) as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(5) from exc
    return CliContext(repo=repo, workspace_root=workspace_root)


@click.group()
@click.version_option(version=_PACKAGE_VERSION)
@click.pass_context
def cli(click_ctx: click.Context) -> None:
    """HungerLoop Agent Harness CLI."""
    if click_ctx.obj is None:
        # Real CLI invocation — try to build a default context.
        click_ctx.obj = _default_context()


cli.add_command(new)
cli.add_command(run)
cli.add_command(status)
cli.add_command(report)
cli.add_command(repair_state)
cli.add_command(hunger)
cli.add_command(memory)
cli.add_command(mission)
cli.add_command(skill)
cli.add_command(trace)
cli.add_command(workspace)
cli.add_command(checks)
