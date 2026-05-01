"""HungerLoop CLI entry point.

Provides ``hungerloop`` command with subcommands for workspace inspection
and check status reporting.
"""
from __future__ import annotations

import click

from hungerloop.cli.checks_cmd import checks
from hungerloop.cli.workspace_cmd import workspace


@click.group()
@click.version_option(version="0.4.1")
def cli() -> None:
    """HungerLoop Agent Harness CLI."""


cli.add_command(workspace)
cli.add_command(checks)
