"""Tests for stagnation exemption with refactor transactions (VAL-REF-016).

Covers:
- While a matching open transaction exists, stagnation exempts only
  declared regression check keys
- Undeclared attempted checks still count
- Exemption stops after closed_success or rolled_back
- Disabled policy provides no exemption
"""
from __future__ import annotations

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import HungerItemStatus, ValidationVerdict
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.refactor import RefactorTransaction, RefactorTransactionStatus
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.stagnation_detector import StagnationDetector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ledger(task_id: str = "task-1", items: list[HungerItem] | None = None) -> HungerLedger:
    return HungerLedger(task_id=task_id, items=items or [])


def _make_item(
    item_id: str = "H-001",
    status: HungerItemStatus = HungerItemStatus.OPEN,
    consecutive_failures: int = 0,
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        status=status,
        consecutive_failure_count=consecutive_failures,
    )


def _make_report(
    task_id: str = "task-1",
    loop_id: int = 10,
    newly_passed: list[str] | None = None,
    attempted: list[str] | None = None,
    has_progress: bool = False,
) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        candidate_state_id=f"CAND-{task_id}-{loop_id}",
        baseline_state_id=None,
        verdict=ValidationVerdict.PASS if has_progress else ValidationVerdict.FAIL,
        newly_passed_check_keys=newly_passed or [],
        attempted_hunger_item_ids=attempted or [],
        has_real_progress=has_progress,
    )


def _make_best_state(task_id: str = "task-1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="BEST-001",
        summary="baseline",
        accepted_check_keys=["H-001:0", "H-002:0"],
    )


def _make_open_txn(
    task_id: str = "task-1",
    declared_keys: list[str] | None = None,
) -> RefactorTransaction:
    return RefactorTransaction(
        transaction_id="txn-001",
        task_id=task_id,
        opening_loop=5,
        deadline_loop=8,
        declared_regression_keys=declared_keys or ["H-001:0"],
        baseline_accepted_check_keys=["H-001:0", "H-002:0"],
        baseline_accepted_check_count=2,
        baseline_best_state=_make_best_state(task_id),
        snapshot_path=".txn_txn-001",
        status=RefactorTransactionStatus.OPEN,
    )


@pytest.fixture
def repo() -> InMemoryRepository:
    r = InMemoryRepository()
    r.create_task("task-1", "test")
    r.set_hunger_policy("task-1", HungerPolicy(refactor_transactions_enabled=True))
    return r


@pytest.fixture
def detector(repo: InMemoryRepository) -> StagnationDetector:
    return StagnationDetector(repo=repo)


# ---------------------------------------------------------------------------
# VAL-REF-016: Stagnation ignores only declared regression items while open
# ---------------------------------------------------------------------------


class TestStagnationExemption:
    """Stagnation exemption applies only to declared regression check keys."""

    def test_open_txn_exempts_declared_regression_items(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """When an open transaction declares H-001:0, that check key's
        regression does not count toward stagnation."""
        item = _make_item("H-001")
        ledger = _make_ledger(items=[item])
        repo.save_hunger_ledger("task-1", ledger)

        txn = _make_open_txn(declared_keys=["H-001:0"])
        repo.save_refactor_transaction(txn)

        # H-001 was attempted, no newly passed, no progress
        report = _make_report(
            attempted=["H-001"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001"],
        )

        # H-001 should not be blocked despite consecutive failures
        # because its only failing check (H-001:0) is a declared regression key
        assert "H-001" not in result["blocked_items"]

    def test_undeclared_attempted_checks_still_count(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """Undeclared failing checks still count toward stagnation."""
        # Make the item already near the threshold
        item = _make_item("H-001", consecutive_failures=9)
        item2 = _make_item("H-002", consecutive_failures=9)
        ledger = _make_ledger(items=[item, item2])
        repo.save_hunger_ledger("task-1", ledger)

        # Transaction only declares H-001:0, not H-002:0
        txn = _make_open_txn(declared_keys=["H-001:0"])
        repo.save_refactor_transaction(txn)

        # Both items attempted, no progress
        report = _make_report(
            attempted=["H-001", "H-002"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001", "H-002"],
        )

        # H-002 should be blocked (undeclared, hit threshold)
        assert "H-002" in result["blocked_items"]

    def test_exemption_stops_after_close(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """Exemption stops after transaction is closed_success."""
        item = _make_item("H-001", consecutive_failures=9)
        ledger = _make_ledger(items=[item])
        repo.save_hunger_ledger("task-1", ledger)

        # Closed transaction should not provide exemption
        txn = _make_open_txn(declared_keys=["H-001:0"])
        closed_txn = txn.model_copy(update={
            "status": RefactorTransactionStatus.CLOSED_SUCCESS,
            "closed_loop": 9,
        })
        repo.save_refactor_transaction(closed_txn)

        report = _make_report(
            attempted=["H-001"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001"],
        )

        # H-001 should be blocked (no active exemption)
        assert "H-001" in result["blocked_items"]

    def test_exemption_stops_after_rollback(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """Exemption stops after transaction is rolled_back."""
        item = _make_item("H-001", consecutive_failures=9)
        ledger = _make_ledger(items=[item])
        repo.save_hunger_ledger("task-1", ledger)

        txn = _make_open_txn(declared_keys=["H-001:0"])
        rolled_back_txn = txn.model_copy(update={
            "status": RefactorTransactionStatus.ROLLED_BACK,
            "closed_loop": 9,
        })
        repo.save_refactor_transaction(rolled_back_txn)

        report = _make_report(
            attempted=["H-001"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001"],
        )

        assert "H-001" in result["blocked_items"]

    def test_disabled_policy_no_exemption(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """Disabled policy provides no exemption even with stale open row."""
        repo.set_hunger_policy(
            "task-1",
            HungerPolicy(refactor_transactions_enabled=False),
        )

        item = _make_item("H-001", consecutive_failures=9)
        ledger = _make_ledger(items=[item])
        repo.save_hunger_ledger("task-1", ledger)

        # Stale open transaction
        txn = _make_open_txn(declared_keys=["H-001:0"])
        repo.save_refactor_transaction(txn)

        report = _make_report(
            attempted=["H-001"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001"],
        )

        # H-001 should be blocked (disabled policy, no exemption)
        assert "H-001" in result["blocked_items"]

    def test_mixed_declared_undeclared_on_same_item(
        self,
        repo: InMemoryRepository,
        detector: StagnationDetector,
    ) -> None:
        """If an item has both declared and undeclared failing checks,
        the undeclared checks still count toward stagnation."""
        # H-001 has two checks: H-001:0 (declared) and H-001:1 (undeclared)
        item = _make_item("H-001", consecutive_failures=9)
        ledger = _make_ledger(items=[item])
        repo.save_hunger_ledger("task-1", ledger)

        # Only H-001:0 is declared, H-001:1 is not
        txn = _make_open_txn(declared_keys=["H-001:0"])
        repo.save_refactor_transaction(txn)

        # The item was attempted, and since H-001:1 is undeclared and failing,
        # the item should still count
        report = _make_report(
            attempted=["H-001"],
            has_progress=False,
        )

        result = detector.update(
            "task-1",
            10,
            report,
            attempted_hunger_item_ids=["H-001"],
        )

        # H-001 should be blocked because it has undeclared failing checks
        # (item-level: not all failing checks are declared)
        assert "H-001" in result["blocked_items"]
