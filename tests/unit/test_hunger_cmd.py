from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.events import EventType
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository


@pytest.fixture(params=["memory", "sqlite"])
def context(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CliContext]:
    repo = (
        SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
        if request.param == "sqlite"
        else InMemoryRepository()
    )
    repo.create_task("T-freeze", "Freeze mission task")
    yield CliContext(repo=repo, workspace_root=tmp_path)
    if isinstance(repo, SQLiteRepository):
        repo.close()


def test_hunger_freeze_marks_task_human_paused(context: CliContext) -> None:
    result = CliRunner().invoke(
        cli,
        ["hunger", "freeze", "T-freeze"],
        obj=context,
    )

    assert result.exit_code == 0, result.output
    assert context.repo.get_hunger_clock("T-freeze").frozen is True
    task = context.repo.get_task("T-freeze")
    assert task is not None
    assert task.status == "HUMAN_PAUSED"
    assert task.last_stop_reason is None
    assert [
        event["event_type"]
        for event in context.repo.list_events("T-freeze")
        if event["event_type"] == EventType.HUNGER_FROZEN.value
    ] == [EventType.HUNGER_FROZEN.value]
