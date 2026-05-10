"""Unit tests for deterministic v0.5f.4 refinement item generation."""
from __future__ import annotations

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import CompletionMode, HungerItemStatus
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.refinement_compiler import RefinementCompiler


def _policy(max_tier: int = 2) -> HungerPolicy:
    return HungerPolicy(
        completion_mode=CompletionMode.SPEND_BUDGET,
        refinement_profile="python_medium",
        max_refinement_tier=max_tier,
    )


def test_adds_tier1_after_base_done() -> None:
    repo = InMemoryRepository()
    base = HungerItem(
        id="H-001",
        title="base",
        status=HungerItemStatus.VALIDATED_SATISFIED,
        gap_score=0.0,
    )
    ledger = HungerLedger(task_id="t1", items=[base])
    repo.save_hunger_ledger("t1", ledger)

    result = RefinementCompiler(repo).ensure_next_tier(
        task_id="t1",
        policy=_policy(max_tier=1),
        ledger=ledger,
        best_state=BestState(
            task_id="t1",
            state_id="BS-1",
            summary="base done",
            accepted_check_keys=["H-001:0"],
        ),
    )

    assert result.added_item_ids == ["H-REF-01-tests"]
    item = repo.get_hunger_item("H-REF-01-tests")
    assert item is not None
    assert item.refinement_tier == 1
    assert item.refinement_kind == "tests"
    assert item.generated_by == "python_medium"
    assert item.source_check_keys == ["H-001:0"]
    assert item.acceptance_checks[0].params["argv"] == ["python", "-m", "pytest", "-q"]


def test_idempotent_when_tier_already_exists() -> None:
    repo = InMemoryRepository()
    base = HungerItem(
        id="H-001",
        title="base",
        status=HungerItemStatus.VALIDATED_SATISFIED,
        gap_score=0.0,
    )
    tier1 = HungerItem(id="H-REF-01-tests", title="tests", refinement_tier=1)
    ledger = HungerLedger(task_id="t1", items=[base, tier1])
    repo.save_hunger_ledger("t1", ledger)

    result = RefinementCompiler(repo).ensure_next_tier(
        task_id="t1",
        policy=_policy(max_tier=1),
        ledger=ledger,
        best_state=None,
    )

    assert result.added_item_ids == []
    assert result.active_tier == 1
    assert [item.id for item in repo.get_hunger_ledger("t1").items] == [
        "H-001",
        "H-REF-01-tests",
    ]


def test_adds_tier2_after_tier1_done() -> None:
    repo = InMemoryRepository()
    base = HungerItem(
        id="H-001",
        title="base",
        status=HungerItemStatus.VALIDATED_SATISFIED,
        gap_score=0.0,
    )
    tier1 = HungerItem(
        id="H-REF-01-tests",
        title="tests",
        status=HungerItemStatus.VALIDATED_SATISFIED,
        gap_score=0.0,
        refinement_tier=1,
    )
    ledger = HungerLedger(task_id="t1", items=[base, tier1])
    repo.save_hunger_ledger("t1", ledger)

    result = RefinementCompiler(repo).ensure_next_tier(
        task_id="t1",
        policy=_policy(max_tier=2),
        ledger=ledger,
        best_state=None,
    )

    assert result.added_item_ids == ["H-REF-02-ruff", "H-REF-02-mypy"]
    assert all(
        repo.get_hunger_item(item_id).refinement_tier == 2  # type: ignore[union-attr]
        for item_id in result.added_item_ids
    )


def test_does_not_generate_before_base_done() -> None:
    repo = InMemoryRepository()
    base = HungerItem(id="H-001", title="base")
    ledger = HungerLedger(task_id="t1", items=[base])
    repo.save_hunger_ledger("t1", ledger)

    result = RefinementCompiler(repo).ensure_next_tier(
        task_id="t1",
        policy=_policy(max_tier=1),
        ledger=ledger,
        best_state=None,
    )

    assert result.added_item_ids == []
    assert result.active_tier == 0
