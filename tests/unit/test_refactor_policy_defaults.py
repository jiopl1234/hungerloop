"""VAL-REF-002: Refactor transaction policy defaults preserve v0.6 behavior.

Verifies that the default policy has refactor_transactions_enabled=False,
max_declared_regressions=5, refactor_deadline_loops=3, and that policy
serialization plus in-memory and SQLite repository round-trips preserve
those values without enabling transaction behavior implicitly.
"""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.hunger import HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository  # noqa: F401

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_default_refactor_transactions_disabled() -> None:
    policy = HungerPolicy()
    assert policy.refactor_transactions_enabled is False


def test_default_max_declared_regressions() -> None:
    policy = HungerPolicy()
    assert policy.max_declared_regressions == 5


def test_default_refactor_deadline_loops() -> None:
    policy = HungerPolicy()
    assert policy.refactor_deadline_loops == 3


# ---------------------------------------------------------------------------
# Serialization preserves defaults
# ---------------------------------------------------------------------------


def test_serialization_preserves_refactor_defaults() -> None:
    policy = HungerPolicy()
    raw = policy.model_dump_json()
    restored = HungerPolicy.model_validate_json(raw)
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3


def test_serialization_preserves_refactor_overrides() -> None:
    policy = HungerPolicy(
        refactor_transactions_enabled=True,
        max_declared_regressions=3,
        refactor_deadline_loops=5,
    )
    raw = policy.model_dump_json()
    restored = HungerPolicy.model_validate_json(raw)
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 3
    assert restored.refactor_deadline_loops == 5


# ---------------------------------------------------------------------------
# In-memory repository round-trip
# ---------------------------------------------------------------------------


def test_in_memory_default_policy_refactor_round_trip() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    policy = HungerPolicy()
    repo.set_hunger_policy("t1", policy)
    restored = repo.get_hunger_policy("t1")
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3


def test_in_memory_override_policy_refactor_round_trip() -> None:
    repo = InMemoryRepository()
    repo.create_task("t1", "Goal")
    policy = HungerPolicy(
        refactor_transactions_enabled=True,
        max_declared_regressions=10,
        refactor_deadline_loops=7,
    )
    repo.set_hunger_policy("t1", policy)
    restored = repo.get_hunger_policy("t1")
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 10
    assert restored.refactor_deadline_loops == 7


# ---------------------------------------------------------------------------
# SQLite repository round-trip
# ---------------------------------------------------------------------------


def test_sqlite_default_policy_refactor_round_trip(tmp_path: Path) -> None:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("t1", "Goal")
    policy = HungerPolicy()
    repo.set_hunger_policy("t1", policy)

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    restored = reopened.get_hunger_policy("t1")
    assert restored.refactor_transactions_enabled is False
    assert restored.max_declared_regressions == 5
    assert restored.refactor_deadline_loops == 3
    reopened.close()


def test_sqlite_override_policy_refactor_round_trip(tmp_path: Path) -> None:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task("t1", "Goal")
    policy = HungerPolicy(
        refactor_transactions_enabled=True,
        max_declared_regressions=10,
        refactor_deadline_loops=7,
    )
    repo.set_hunger_policy("t1", policy)

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    restored = reopened.get_hunger_policy("t1")
    assert restored.refactor_transactions_enabled is True
    assert restored.max_declared_regressions == 10
    assert restored.refactor_deadline_loops == 7
    reopened.close()
