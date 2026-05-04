"""``hungerloop memory list --state`` filter tests (PRD §19.1).

The CLI surfaces the v0.5c.0 lifecycle states (``proposed`` / ``approved``
/ ``rejected`` / ``expired`` / ``superseded``) plus a default ``all``.
v0.5c only ever *writes* ``proposed``, but the filter itself is fully
plumbed so v0.6 promotion lands without CLI churn.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.memory import MemoryCandidate
from hungerloop.repository.in_memory_repo import InMemoryRepository


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    repo = InMemoryRepository()
    repo.save_memory_candidate(
        MemoryCandidate(
            candidate_id="mem-prop-1",
            task_id="task-1",
            content="proposed candidate",
            state="proposed",
        )
    )
    # Synthetic approved row — v0.5c never emits this, but the CLI filter
    # must still hide / surface it. Future v0.6 promotion will create
    # rows like this through a proper code path.
    repo.save_memory_candidate(
        MemoryCandidate(
            candidate_id="mem-approved-1",
            task_id="task-1",
            content="approved candidate",
            state="approved",
        )
    )
    return CliContext(repo=repo, workspace_root=tmp_path)


def test_memory_list_default_returns_all_states(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "list", "task-1"], obj=context)
    assert result.exit_code == 0
    assert "mem-prop-1" in result.output
    assert "mem-approved-1" in result.output
    # State tag is rendered alongside the predicate flags.
    assert "[proposed]" in result.output
    assert "[approved]" in result.output


def test_memory_list_state_proposed_filters_correctly(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["memory", "list", "task-1", "--state", "proposed"], obj=context
    )
    assert result.exit_code == 0
    assert "mem-prop-1" in result.output
    assert "mem-approved-1" not in result.output


def test_memory_list_state_approved_filters_correctly(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["memory", "list", "task-1", "--state", "approved"], obj=context
    )
    assert result.exit_code == 0
    assert "mem-approved-1" in result.output
    assert "mem-prop-1" not in result.output


def test_memory_list_state_all_includes_everything(
    context: CliContext,
) -> None:
    """``--state all`` is identical to omitting the flag."""
    runner = CliRunner()
    result_all = runner.invoke(
        cli, ["memory", "list", "task-1", "--state", "all"], obj=context
    )
    result_default = runner.invoke(
        cli, ["memory", "list", "task-1"], obj=context
    )
    assert result_all.exit_code == 0
    assert result_default.exit_code == 0
    assert result_all.output == result_default.output


def test_memory_list_state_with_no_matches_says_so(
    context: CliContext,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "list", "task-1", "--state", "expired"],
        obj=context,
    )
    assert result.exit_code == 0
    assert "No memory candidates" in result.output


def test_memory_list_rejects_unknown_state(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["memory", "list", "task-1", "--state", "promoted"],
        obj=context,
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "promoted" in result.output
