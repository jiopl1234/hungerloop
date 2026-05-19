from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hungerloop.models.worker import HandoffItem, WorkerHandoff, WorkerResult
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository

RepoUnderTest = InMemoryRepository | SQLiteRepository


def _make_worker_handoff(
    *,
    task_id: str = "task-1",
    loop_id: int,
    agent_id: str = "execution_worker_v1",
    summary: str | None = None,
) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id=agent_id,
        task_id=task_id,
        loop_id=loop_id,
        summary=summary or f"handoff-{loop_id}",
        artifact_ids=[f"artifact-{loop_id}"],
        evidence_ids=[f"evidence-{loop_id}"],
        claim_ids=[f"claim-{loop_id}"],
        llm_call_ids=[f"llm-{loop_id}"],
        tool_call_ids=[f"tool-{loop_id}"],
        handoff_items=[
            HandoffItem(
                item_type="follow_up",
                summary=f"follow-up-{loop_id}",
                detail=f"detail-{loop_id}",
                related_item_ids=[f"H-{loop_id}"],
            )
        ],
        what_was_done=[f"completed-{loop_id}"],
        what_was_left_undone=[f"remaining-{loop_id}"],
        verification_commands=[f"pytest -q handoff-{loop_id}"],
        next_worker_hint=f"hint-{loop_id}",
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "in_memory":
        repository: RepoUnderTest = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Goal")
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


def test_roundtrip(repo: RepoUnderTest) -> None:
    handoff = _make_worker_handoff(loop_id=1)

    handoff_id = repo.save_worker_handoff(handoff)

    assert handoff_id
    assert repo.list_worker_handoffs("task-1") == [handoff]


def test_filters(repo: RepoUnderTest) -> None:
    repo.create_task("task-2", "Other goal")

    for loop_id in range(1, 6):
        repo.save_worker_handoff(_make_worker_handoff(loop_id=loop_id))
    repo.save_worker_handoff(_make_worker_handoff(task_id="task-2", loop_id=99))

    filtered = repo.list_worker_handoffs("task-1", since_loop_id=3, limit=2)

    assert [handoff.loop_id for handoff in filtered] == [3, 4]


def test_get_last_handoff_loop_boundary(repo: RepoUnderTest) -> None:
    earlier = _make_worker_handoff(loop_id=2)
    excluded = _make_worker_handoff(loop_id=4, summary="too-late")
    other_agent = _make_worker_handoff(agent_id="research_worker_v1", loop_id=3)

    repo.save_worker_handoff(earlier)
    repo.save_worker_handoff(excluded)
    repo.save_worker_handoff(other_agent)

    assert (
        repo.get_last_worker_handoff("task-1", "execution_worker_v1", before_loop_id=4)
        == earlier
    )
    assert (
        repo.get_last_worker_handoff("task-1", "execution_worker_v1", before_loop_id=2)
        is None
    )


def test_get_last_handoff_and_result_consistency(repo: RepoUnderTest) -> None:
    repo.save_worker_handoff(_make_worker_handoff(loop_id=1, summary="first"))
    expected = _make_worker_handoff(loop_id=3, summary="third")
    repo.save_worker_handoff(expected)

    last_handoff = repo.get_last_worker_handoff(
        "task-1",
        "execution_worker_v1",
        before_loop_id=4,
    )
    last_result = repo.get_last_worker_result(
        "task-1",
        "execution_worker_v1",
        4,
    )

    assert last_handoff == expected
    assert last_result is not None
    assert last_handoff.as_worker_result().model_dump() == last_result.model_dump()


def test_get_last_worker_result_falls_back_to_legacy_storage(
    repo: RepoUnderTest,
) -> None:
    legacy = WorkerResult(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=2,
        summary="legacy",
    )

    repo.save_worker_result(legacy)

    assert repo.get_last_worker_result("task-1", "execution_worker_v1", 3) == legacy


def test_sqlite_save_worker_handoff_persists_payload_json(tmp_path: Path) -> None:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("task-1", "Goal")
    handoff = _make_worker_handoff(loop_id=5)

    handoff_id = repo.save_worker_handoff(handoff)

    row = repo.conn.execute(
        """
        SELECT task_id, loop_id, agent_id, payload_json
        FROM worker_handoffs
        WHERE handoff_id = ?
        """,
        (handoff_id,),
    ).fetchone()

    assert row is not None
    assert str(row["task_id"]) == "task-1"
    assert int(row["loop_id"]) == 5
    assert str(row["agent_id"]) == "execution_worker_v1"
    assert WorkerHandoff.model_validate_json(str(row["payload_json"])) == handoff

    repo.close()
