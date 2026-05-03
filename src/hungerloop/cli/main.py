"""HungerLoop CLI entry point.

v0.5a CLI surface (PRD §18):

* ``hungerloop new`` — compile a task from goal + acceptance specs.
* ``hungerloop run`` — preflight then drive the orchestrator.
* ``hungerloop status`` — print task summary.
* ``hungerloop hunger {refill,unblock,unblock-all,freeze,resume}`` —
  human operations on hunger state.
* ``hungerloop workspace`` / ``hungerloop checks`` — pre-existing
  inspection helpers (v0.4.1).

Production users currently cannot invoke the v0.5a commands: there is no
``SQLiteRepository`` yet, so the default context factory raises with a
clear message. Tests inject a :class:`CliContext` carrying an
:class:`InMemoryRepository`.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import click

from hungerloop.cli.checks_cmd import checks
from hungerloop.cli.context import CliContext
from hungerloop.cli.hunger_cmd import hunger
from hungerloop.cli.memory_cmd import memory
from hungerloop.cli.new_cmd import new
from hungerloop.cli.run_cmd import run
from hungerloop.cli.skill_cmd import skill
from hungerloop.cli.status_cmd import status
from hungerloop.cli.workspace_cmd import workspace

try:
    _PACKAGE_VERSION = version("hungerloop")
except PackageNotFoundError:
    # Editable install / source checkout without metadata: fall back gracefully.
    _PACKAGE_VERSION = "0+source"


def _default_context() -> CliContext:
    """Build the default :class:`CliContext` for production use.

    Raises:
        click.ClickException: v0.5a does not ship :class:`SQLiteRepository`
            yet; the CLI cannot create a persistence-backed context outside
            of tests. Wire SQLiteRepository here when it lands.
    """
    raise click.ClickException(
        "v0.5a CLI cannot run without SQLiteRepository yet. "
        "Tests inject CliContext with InMemoryRepository via runner.invoke(obj=...). "
        "SQLiteRepository ships in v0.5b; until then the CLI is library-only."
    )


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
cli.add_command(hunger)
cli.add_command(memory)
cli.add_command(skill)
cli.add_command(workspace)
cli.add_command(checks)
