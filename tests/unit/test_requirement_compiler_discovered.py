from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.handoff import DiscoveredFact
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.requirement_compiler import RequirementCompiler

RepoUnderTest = InMemoryRepository | SQLiteRepository


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE, max_new_items_per_loop=3)


def _fact(*, title: str = "Add validation smoke") -> DiscoveredFact:
    return DiscoveredFact(
        kind="mission_feature",
        title=title,
        description="Need a deterministic validator for the new surface.",
        source_handoff_id="WH-task-1-3-execution_worker_v1",
        related_feature_ids=["feature-1"],
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "in_memory":
        repository: RepoUnderTest = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Compile discovered facts")
    repository.save_hunger_ledger("task-1", HungerLedger(task_id="task-1", items=[]))
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


def test_compile_discovered_priorities_and_gap(repo: RepoUnderTest) -> None:
    compiler = RequirementCompiler(repo)

    injected_ids = compiler.compile_discovered_facts(
        "task-1",
        [_fact()],
        budget=_budget(),
    )

    assert len(injected_ids) == 1
    item = repo.get_hunger_item(injected_ids[0])
    assert item is not None
    assert item.priority == pytest.approx(0.8)
    assert item.gap_score == 1.0
    assert item.refinement_tier == 0
    assert len(item.acceptance_checks) == 1
    check = item.acceptance_checks[0]
    assert check.check_type == AcceptanceCheckType.EVIDENCE_COUNT_MIN
    assert check.params["evidence_type"] == "discovered_fact_compiled"
    assert check.params["min_count"] == 1
    assert item.evidence_ids
    assert repo.count_evidence_by_type(
        "task-1",
        item.evidence_ids,
        "discovered_fact_compiled",
        successful_only=True,
    ) == 1


def test_compile_discovered_facts_is_idempotent(repo: RepoUnderTest) -> None:
    compiler = RequirementCompiler(repo)
    fact = _fact(title="Add blocking note")

    first_ids = compiler.compile_discovered_facts("task-1", [fact], budget=_budget())
    second_ids = compiler.compile_discovered_facts("task-1", [fact], budget=_budget())

    assert len(first_ids) == 1
    assert second_ids == []
    ledger = repo.get_hunger_ledger("task-1")
    assert [item.id for item in ledger.items] == first_ids
