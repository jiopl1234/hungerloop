from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.requirement_compiler import RequirementCompiler

RepoUnderTest = InMemoryRepository | SQLiteRepository


def _budget(max_new_items_per_loop: int = 3) -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        max_new_items_per_loop=max_new_items_per_loop,
    )


def _handoff(*items: HandoffItem) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=3,
        summary="Completed a partial pass and left structured handoff notes.",
        handoff_items=list(items),
    )


def _phase() -> MissionPhase:
    return MissionPhase(
        phase_id="phase-1",
        title="Phase 1",
        description="Initial milestone",
        feature_ids=["feature-1", "feature-2"],
        validation_contract_ids=[],
    )


def _feature(feature_id: str, *, status: str = "pending") -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=f"H-{feature_id}",
        phase_id="phase-1",
        title=f"Feature {feature_id}",
        description=f"Description for {feature_id}",
        preconditions=[],
        expected_behavior=[],
        verification_steps=[],
        fulfills=[],
        status=status,
    )


def _mission(*, feature_statuses: dict[str, str] | None = None) -> Mission:
    statuses = feature_statuses or {}
    features = [
        _feature("feature-1", status=statuses.get("feature-1", "pending")),
        _feature("feature-2", status=statuses.get("feature-2", "pending")),
    ]
    return Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission 1",
        description="Mission for handoff processing",
        phases=[_phase()],
        features=features,
        created_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
    )


def _save_mission(repo: RepoUnderTest, mission: Mission) -> None:
    repo.save_mission(mission)
    for phase in mission.phases:
        repo.save_mission_phase(phase)
    for feature in mission.features:
        repo.save_mission_feature(feature)


class RejectingRequirementCompiler(RequirementCompiler):
    def compile_discovered_facts(
        self,
        task_id: str,
        facts: list[object],
        *,
        budget: BudgetAllocation,
    ) -> list[str]:
        del task_id, facts, budget
        from hungerloop.models.handoff import DiscoveredFact

        DiscoveredFact(
            kind="quantum_judge",  # type: ignore[arg-type]
            title="invalid",
            description="invalid",
            source_handoff_id="bad",
        )
        raise AssertionError("unreachable")


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "in_memory":
        repository: RepoUnderTest = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Process worker handoffs")
    repository.save_hunger_ledger("task-1", HungerLedger(task_id="task-1", items=[]))
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


def _event_types(repo: RepoUnderTest, *, task_id: str = "task-1") -> list[str]:
    return [str(row["event_type"]) for row in repo.list_events(task_id)]


@pytest.mark.asyncio
async def test_discovered_issue_cap(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    handoff = _handoff(
        *[
            HandoffItem(
                item_type="discovered_issue",
                summary=f"issue-{index}",
                detail=f"detail-{index}",
                related_feature_ids=["feature-1"],
            )
            for index in range(5)
        ]
    )

    result = await processor.process_handoffs(
        "task-1",
        3,
        [handoff],
        mission=None,
        budget=_budget(3),
    )

    assert len(result.injected_hunger_item_ids) == 3
    assert len(repo.get_hunger_ledger("task-1").items) == 3


@pytest.mark.asyncio
async def test_discovered_issue_cap_demotion_text(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    handoff = _handoff(
        *[
            HandoffItem(
                item_type="discovered_issue",
                summary=f"issue-{index}",
                detail=f"detail-{index}",
                related_feature_ids=["feature-1"],
            )
            for index in range(5)
        ]
    )

    result = await processor.process_handoffs(
        "task-1",
        3,
        [handoff],
        mission=None,
        budget=_budget(3),
    )

    assert result.prior_handoff_summary.count("Follow-up:") >= 2


@pytest.mark.asyncio
async def test_follow_up_prefix(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    result = await processor.process_handoffs(
        "task-1",
        3,
        [
            _handoff(
                HandoffItem(
                    item_type="follow_up",
                    summary="check X",
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    assert "Follow-up: check X" in result.prior_handoff_summary


@pytest.mark.asyncio
async def test_critical_context_prefix(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    result = await processor.process_handoffs(
        "task-1",
        3,
        [
            _handoff(
                HandoffItem(
                    item_type="follow_up",
                    summary="finish happy-path checks",
                ),
                HandoffItem(
                    item_type="critical_context",
                    summary="DB schema mismatch",
                ),
            )
        ],
        mission=None,
        budget=_budget(),
    )

    assert result.prior_handoff_summary.startswith("[CRITICAL] DB schema mismatch")


@pytest.mark.asyncio
async def test_incomplete_work_feature_update(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    mission = _mission()
    _save_mission(repo, mission)

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    await processor.process_handoffs(
        "task-1",
        3,
        [
            _handoff(
                HandoffItem(
                    item_type="incomplete_work",
                    related_feature_ids=["feature-1", "feature-2"],
                )
            )
        ],
        mission=mission,
        budget=_budget(),
    )

    statuses = {
        feature.feature_id: feature.status
        for feature in repo.list_mission_features(mission_id="mission-1")
    }
    assert statuses == {"feature-1": "in_progress", "feature-2": "in_progress"}


@pytest.mark.asyncio
async def test_incomplete_work_idempotent(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    mission = _mission(feature_statuses={"feature-1": "in_progress"})
    _save_mission(repo, mission)

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    await processor.process_handoffs(
        "task-1",
        3,
        [
            _handoff(
                HandoffItem(
                    item_type="incomplete_work",
                    related_feature_ids=["feature-1"],
                )
            )
        ],
        mission=mission,
        budget=_budget(),
    )

    feature = repo.list_mission_features(mission_id="mission-1", phase_id="phase-1")[0]
    assert feature.feature_id == "feature-1"
    assert feature.status == "in_progress"


@pytest.mark.asyncio
async def test_idempotency_no_duplicate_injection(repo: RepoUnderTest) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(repo, requirement_compiler=RequirementCompiler(repo))
    handoff = _handoff(
        HandoffItem(
            item_type="discovered_issue",
            summary="Need validator coverage",
            detail="Add a deterministic test for the new handoff processor.",
            related_feature_ids=["feature-1"],
        )
    )

    first = await processor.process_handoffs(
        "task-1",
        3,
        [handoff],
        mission=None,
        budget=_budget(),
    )
    second = await processor.process_handoffs(
        "task-1",
        3,
        [handoff],
        mission=None,
        budget=_budget(),
    )

    assert len(first.injected_hunger_item_ids) == 1
    assert second.injected_hunger_item_ids == []
    assert len(repo.get_hunger_ledger("task-1").items) == 1


@pytest.mark.asyncio
async def test_schema_rejected_discovered_fact_demotes_to_follow_up(
    repo: RepoUnderTest,
) -> None:
    from hungerloop.services.handoff_processor import HandoffProcessor

    processor = HandoffProcessor(
        repo,
        requirement_compiler=RejectingRequirementCompiler(repo),
    )

    result = await processor.process_handoffs(
        "task-1",
        3,
        [
            _handoff(
                HandoffItem(
                    item_type="discovered_issue",
                    summary="Need a new validator",
                    detail="This issue should be demoted when fact validation fails.",
                )
            )
        ],
        mission=None,
        budget=_budget(),
    )

    assert result.discovered_issues == []
    assert result.injected_hunger_item_ids == []
    assert "Follow-up: Need a new validator" in result.prior_handoff_summary
    assert "DISCOVERED_FACT_REJECTED" in _event_types(repo)
