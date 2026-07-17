"""Unit tests for v0.7 HungerPolicy flags and repository round-trips.

Covers VAL-SYN-003, VAL-SYN-020, VAL-CROSS-002.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hungerloop.models.hunger import HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository

# ---------------------------------------------------------------------------
# VAL-SYN-003: V0.7 policy flags preserve v0.6 defaults
# ---------------------------------------------------------------------------


def test_default_synthesis_enabled() -> None:
    assert HungerPolicy().synthesis_enabled is False


def test_default_synthesis_plan_time_tier() -> None:
    assert HungerPolicy().synthesis_plan_time_tier == 0


def test_default_synthesis_max_total_items() -> None:
    assert HungerPolicy().synthesis_max_total_items == 20


def test_default_synthesis_lifecycle_policy() -> None:
    policy = HungerPolicy()
    assert policy.synthesis_max_active_items == 3
    assert policy.synthesis_batch_size == 3
    assert policy.synthesis_conflict_threshold == 2
    assert policy.synthesis_audit_enabled is False
    assert policy.regression_confirm_reruns == 2
    assert policy.rejected_candidate_continuation_enabled is True
    assert policy.rejected_candidate_continuation_max_chain == 2


def test_default_refactor_transactions_enabled() -> None:
    assert HungerPolicy().refactor_transactions_enabled is False


def test_default_max_declared_regressions() -> None:
    assert HungerPolicy().max_declared_regressions == 5


def test_default_refactor_deadline_loops() -> None:
    assert HungerPolicy().refactor_deadline_loops == 3


def test_default_memory_auto_promote_enabled() -> None:
    assert HungerPolicy().memory_auto_promote_enabled is True


def test_default_memory_recall_enabled() -> None:
    assert HungerPolicy().memory_recall_enabled is True


def test_v07_policy_flags_present() -> None:
    """All v0.7 policy flags must exist on the model."""
    policy = HungerPolicy()
    flags = [
        "synthesis_enabled",
        "synthesis_plan_time_tier",
        "synthesis_max_total_items",
        "regression_confirm_reruns",
        "rejected_candidate_continuation_enabled",
        "rejected_candidate_continuation_max_chain",
        "refactor_transactions_enabled",
        "max_declared_regressions",
        "refactor_deadline_loops",
        "memory_auto_promote_enabled",
        "memory_recall_enabled",
    ]
    for flag in flags:
        assert hasattr(policy, flag), f"Missing flag: {flag}"


def test_policy_serialization_preserves_defaults() -> None:
    policy = HungerPolicy()
    raw = policy.model_dump_json()
    restored = HungerPolicy.model_validate_json(raw)
    assert restored.synthesis_enabled is False
    assert restored.synthesis_plan_time_tier == 0
    assert restored.synthesis_max_total_items == 20
    assert restored.regression_confirm_reruns == 2
    assert restored.rejected_candidate_continuation_enabled is True
    assert restored.rejected_candidate_continuation_max_chain == 2
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3
    assert restored.memory_auto_promote_enabled is True
    assert restored.memory_recall_enabled is True


def test_policy_serialization_preserves_overrides() -> None:
    policy = HungerPolicy(
        synthesis_enabled=True,
        synthesis_plan_time_tier=2,
        synthesis_max_total_items=10,
        synthesis_max_active_items=4,
        synthesis_batch_size=2,
        synthesis_conflict_threshold=3,
        synthesis_audit_enabled=True,
        regression_confirm_reruns=2,
        rejected_candidate_continuation_enabled=False,
        rejected_candidate_continuation_max_chain=4,
        refactor_transactions_enabled=True,
        max_declared_regressions=3,
        refactor_deadline_loops=5,
        memory_auto_promote_enabled=False,
        memory_recall_enabled=False,
    )
    raw = policy.model_dump_json()
    restored = HungerPolicy.model_validate_json(raw)
    assert restored.synthesis_enabled is True
    assert restored.synthesis_plan_time_tier == 2
    assert restored.synthesis_max_total_items == 10
    assert restored.synthesis_max_active_items == 4
    assert restored.synthesis_batch_size == 2
    assert restored.synthesis_conflict_threshold == 3
    assert restored.synthesis_audit_enabled is True
    assert restored.regression_confirm_reruns == 2
    assert restored.rejected_candidate_continuation_enabled is False
    assert restored.rejected_candidate_continuation_max_chain == 4
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 3
    assert restored.refactor_deadline_loops == 5
    assert restored.memory_auto_promote_enabled is False
    assert restored.memory_recall_enabled is False


def test_regression_confirm_reruns_must_not_be_negative() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(regression_confirm_reruns=-1)


def test_rejected_candidate_continuation_max_chain_must_not_be_negative() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(rejected_candidate_continuation_max_chain=-1)


def test_v06_fields_preserved() -> None:
    """Existing v0.6 fields are still present with correct defaults."""
    policy = HungerPolicy()
    assert policy.initial_hunger == 100.0
    assert policy.h_max == 100.0
    assert policy.max_total_cost_usd == 10.0
    assert policy.max_total_tokens == 1_000_000
    assert policy.completion_mode.value == "stop_on_done"
    assert policy.refinement_profile is None
    assert policy.max_refinement_tier == 0
    assert policy.respect_stagnation is True


# ---------------------------------------------------------------------------
# VAL-SYN-020: Invalid synthesis policy values fail deterministically
# ---------------------------------------------------------------------------


def test_negative_synthesis_plan_time_tier_fails() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(synthesis_plan_time_tier=-1)


def test_negative_synthesis_max_total_items_fails() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(synthesis_max_total_items=-1)


def test_zero_synthesis_max_total_items_valid() -> None:
    """Zero synthesis capacity is valid (no-op)."""
    policy = HungerPolicy(synthesis_max_total_items=0)
    assert policy.synthesis_max_total_items == 0


def test_zero_synthesis_plan_time_tier_valid() -> None:
    """Zero tier is valid (default)."""
    policy = HungerPolicy(synthesis_plan_time_tier=0)
    assert policy.synthesis_plan_time_tier == 0


@pytest.mark.parametrize(
    "field",
    [
        "synthesis_max_active_items",
        "synthesis_batch_size",
        "synthesis_conflict_threshold",
    ],
)
def test_synthesis_lifecycle_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError):
        HungerPolicy(**{field: 0})


def test_negative_max_declared_regressions_fails() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(max_declared_regressions=-1)


def test_negative_refactor_deadline_loops_fails() -> None:
    with pytest.raises(ValidationError):
        HungerPolicy(refactor_deadline_loops=-1)


def test_zero_max_declared_regressions_valid() -> None:
    policy = HungerPolicy(max_declared_regressions=0)
    assert policy.max_declared_regressions == 0


def test_zero_refactor_deadline_loops_valid() -> None:
    policy = HungerPolicy(refactor_deadline_loops=0)
    assert policy.refactor_deadline_loops == 0


# ---------------------------------------------------------------------------
# VAL-CROSS-002: V0.7 policy defaults preserved across repository round-trips
# ---------------------------------------------------------------------------


def test_in_memory_default_policy_round_trip() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    policy = HungerPolicy()
    repo.set_hunger_policy("t1", policy)
    restored = repo.get_hunger_policy("t1")
    assert restored.synthesis_enabled is False
    assert restored.synthesis_plan_time_tier == 0
    assert restored.synthesis_max_total_items == 20
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3
    assert restored.memory_auto_promote_enabled is True
    assert restored.memory_recall_enabled is True


def test_in_memory_override_policy_round_trip() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    policy = HungerPolicy(
        synthesis_enabled=True,
        synthesis_plan_time_tier=2,
        synthesis_max_total_items=10,
        synthesis_max_active_items=4,
        synthesis_batch_size=2,
        synthesis_conflict_threshold=3,
        synthesis_audit_enabled=True,
        regression_confirm_reruns=2,
        refactor_transactions_enabled=True,
        max_declared_regressions=3,
        refactor_deadline_loops=5,
        memory_auto_promote_enabled=False,
        memory_recall_enabled=False,
    )
    repo.set_hunger_policy("t1", policy)
    restored = repo.get_hunger_policy("t1")
    assert restored.synthesis_enabled is True
    assert restored.synthesis_plan_time_tier == 2
    assert restored.synthesis_max_total_items == 10
    assert restored.synthesis_max_active_items == 4
    assert restored.synthesis_batch_size == 2
    assert restored.synthesis_conflict_threshold == 3
    assert restored.synthesis_audit_enabled is True
    assert restored.regression_confirm_reruns == 2
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 3
    assert restored.refactor_deadline_loops == 5
    assert restored.memory_auto_promote_enabled is False
    assert restored.memory_recall_enabled is False


def test_sqlite_default_policy_round_trip(tmp_path: Path) -> None:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("t1", "Goal")
    policy = HungerPolicy()
    repo.set_hunger_policy("t1", policy)

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    restored = reopened.get_hunger_policy("t1")
    assert restored.synthesis_enabled is False
    assert restored.synthesis_plan_time_tier == 0
    assert restored.synthesis_max_total_items == 20
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3
    assert restored.memory_auto_promote_enabled is True
    assert restored.memory_recall_enabled is True
    reopened.close()


def test_sqlite_override_policy_round_trip(tmp_path: Path) -> None:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("t1", "Goal")
    policy = HungerPolicy(
        synthesis_enabled=True,
        synthesis_plan_time_tier=2,
        synthesis_max_total_items=10,
        synthesis_max_active_items=4,
        synthesis_batch_size=2,
        synthesis_conflict_threshold=3,
        synthesis_audit_enabled=True,
        regression_confirm_reruns=2,
        refactor_transactions_enabled=True,
        max_declared_regressions=3,
        refactor_deadline_loops=5,
        memory_auto_promote_enabled=False,
        memory_recall_enabled=False,
    )
    repo.set_hunger_policy("t1", policy)

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    restored = reopened.get_hunger_policy("t1")
    assert restored.synthesis_enabled is True
    assert restored.synthesis_plan_time_tier == 2
    assert restored.synthesis_max_total_items == 10
    assert restored.synthesis_max_active_items == 4
    assert restored.synthesis_batch_size == 2
    assert restored.synthesis_conflict_threshold == 3
    assert restored.synthesis_audit_enabled is True
    assert restored.regression_confirm_reruns == 2
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 3
    assert restored.refactor_deadline_loops == 5
    assert restored.memory_auto_promote_enabled is False
    assert restored.memory_recall_enabled is False
    reopened.close()


def test_sqlite_round_trip_preserves_v06_fields(tmp_path: Path) -> None:
    """Repository round-trip preserves v0.6 fields alongside v0.7 flags."""
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("t1", "Goal")
    policy = HungerPolicy(
        initial_hunger=80.0,
        h_max=90.0,
        max_total_cost_usd=5.0,
        synthesis_enabled=True,
        memory_recall_enabled=False,
    )
    repo.set_hunger_policy("t1", policy)

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    restored = reopened.get_hunger_policy("t1")
    assert restored.initial_hunger == 80.0
    assert restored.h_max == 90.0
    assert restored.max_total_cost_usd == 5.0
    assert restored.synthesis_enabled is True
    assert restored.memory_recall_enabled is False
    reopened.close()


# ---------------------------------------------------------------------------
# v0.7.2: draft_sampling_k policy field
# ---------------------------------------------------------------------------


def test_draft_sampling_k_default_off() -> None:
    assert HungerPolicy().draft_sampling_k == 1


def test_draft_sampling_k_bounds() -> None:
    assert HungerPolicy(draft_sampling_k=5).draft_sampling_k == 5
    with pytest.raises(ValidationError):
        HungerPolicy(draft_sampling_k=0)
    with pytest.raises(ValidationError):
        HungerPolicy(draft_sampling_k=6)


def test_draft_sampling_k_roundtrips_via_payload_json() -> None:
    policy = HungerPolicy(draft_sampling_k=3)
    restored = HungerPolicy.model_validate(policy.model_dump(mode="json"))
    assert restored.draft_sampling_k == 3


# ---------------------------------------------------------------------------
# v0.7.x: configurable global no-progress fuse
# ---------------------------------------------------------------------------


def test_max_global_no_progress_loops_default() -> None:
    assert HungerPolicy().max_global_no_progress_loops == 5


def test_max_global_no_progress_loops_must_be_positive() -> None:
    assert HungerPolicy(max_global_no_progress_loops=3).max_global_no_progress_loops == 3
    with pytest.raises(ValidationError):
        HungerPolicy(max_global_no_progress_loops=0)
