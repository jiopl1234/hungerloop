from __future__ import annotations

from pathlib import Path

import pytest

from hungerloop.models.enums import HungerItemStatus, LoopPhase, StopReason
from hungerloop.models.hunger import HungerItem, HungerLedger
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.requirement_compiler import RequirementCompiler


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE, max_new_items_per_loop=3)


def _handoff(*items: HandoffItem) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=2,
        summary="Blocked on a dependency.",
        handoff_items=list(items),
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> InMemoryRepository | SQLiteRepository:
    if request.param == "in_memory":
        repository: InMemoryRepository | SQLiteRepository = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Exercise blocked propagation")
    repository.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[HungerItem(id="H-001", title="Main task")],
        ),
    )
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


def test_handoff_processor_returns_no_stop_reason(
    repo: InMemoryRepository | SQLiteRepository,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    result = processor.process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="blocker",
                    summary="Waiting on an upstream dependency",
                    related_item_ids=["H-001"],
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    with pytest.raises(AttributeError):
        _ = result.early_stop_reason
    assert hasattr(result, "stop_reason") is False


def test_blocker_sets_status(
    repo: InMemoryRepository | SQLiteRepository,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    result = processor.process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="blocker",
                    summary="Waiting on an upstream dependency",
                    related_item_ids=["H-001"],
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    item = repo.get_hunger_item("H-001")
    assert item is not None
    assert item.status == HungerItemStatus.BLOCKED
    assert result.blocked_item_ids == ["H-001"]


def test_handoff_blocker_propagates_to_engine_blocked(
    repo: InMemoryRepository | SQLiteRepository,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    processor.process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="blocker",
                    summary="Waiting on an upstream dependency",
                    related_item_ids=["H-001"],
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    snapshot = HungerEngine().tick(
        repo.get_hunger_policy("task-1"),
        repo.get_hunger_clock("task-1"),
        repo.get_hunger_ledger("task-1"),
    )

    assert snapshot.stop_reason == StopReason.BLOCKED


def test_handoff_blocker_recorded_event_emitted_per_blocked_item(
    repo: InMemoryRepository | SQLiteRepository,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(id="H-001", title="Main task"),
                HungerItem(id="H-002", title="Secondary task"),
            ],
        ),
    )
    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    processor.process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="blocker",
                    summary="First blocker",
                    related_item_ids=["H-001"],
                ),
                HandoffItem(
                    item_type="blocker",
                    summary="Second blocker",
                    related_item_ids=["H-002"],
                ),
            )
        ],
        mission=None,
        budget=_budget(),
    )

    events = repo.list_events(
        "task-1",
        event_types=["worker.handoff_blocker_recorded"],
    )
    assert len(events) == 2
    assert [event["payload"]["item_id"] for event in events] == ["H-001", "H-002"]


def test_blocker_on_closed_item_warns_without_reopening(
    repo: InMemoryRepository | SQLiteRepository,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(
                    id="H-001",
                    title="Already closed",
                    status=HungerItemStatus.CLOSED,
                )
            ],
        ),
    )
    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))

    processor.process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="blocker",
                    summary="Late blocker",
                    related_item_ids=["H-001"],
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    item = repo.get_hunger_item("H-001")
    assert item is not None
    assert item.status == HungerItemStatus.CLOSED
    events = repo.list_events(
        "task-1",
        event_types=["HANDOFF_BLOCKER_ON_CLOSED_ITEM"],
    )
    assert len(events) == 1
    assert events[0]["payload"]["item_id"] == "H-001"
