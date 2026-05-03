"""Click context object shared by all v0.5a CLI commands.

Each command takes a :class:`CliContext` via :func:`click.pass_obj`. The
context carries the :class:`RepositoryProtocol` instance and the
workspace root path so subcommands don't have to know how the repo was
constructed.

In v0.5a the production entry point cannot yet build a real context —
:class:`SQLiteRepository` is not implemented. The default factory in
:mod:`hungerloop.cli.main` raises with a clear message; tests inject a
:class:`CliContext` carrying an :class:`InMemoryRepository`.
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
