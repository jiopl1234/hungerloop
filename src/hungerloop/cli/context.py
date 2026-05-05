"""Click context object shared by all CLI commands.

Each command takes a :class:`CliContext` via :func:`click.pass_obj`. The
context carries the :class:`RepositoryProtocol` instance and the
workspace root path so subcommands don't have to know how the repo was
constructed.

Production entry points build a SQLite-backed context in
:mod:`hungerloop.cli.main`; tests can inject an in-memory context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.model_client import ModelClient


@dataclass
class CliContext:
    """Shared dependencies for all CLI subcommands."""

    repo: RepositoryProtocol
    workspace_root: Path
    model_client: ModelClient | None = None
    extras: dict[str, object] = field(default_factory=dict)
