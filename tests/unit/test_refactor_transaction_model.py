"""Unit tests for RefactorTransaction model (VAL-REF-001).

Covers model validation: required fields, lifecycle status integrity,
identifier validation, declared regression keys, loop/deadline values,
baseline payloads, snapshot paths, and serialization round-trips.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hungerloop.models.blackboard import BestState
from hungerloop.models.refactor import (
    RefactorTransaction,
    RefactorTransactionStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_baseline_best_state(task_id: str = "task-1") -> BestState:
    return BestState(
        task_id=task_id,
        state_id="bs-001",
        summary="baseline state",
        accepted_check_keys=["H-001:0", "H-002:0"],
        updated_at_loop=3,
    )


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    """Return kwargs for a valid open RefactorTransaction."""
    base: dict[str, object] = {
        "transaction_id": "txn-001",
        "task_id": "task-1",
        "opening_loop": 5,
        "deadline_loop": 8,
        "declared_regression_keys": ["H-001:0", "H-002:0"],
        "baseline_accepted_check_keys": ["H-001:0", "H-002:0"],
        "baseline_accepted_check_count": 2,
        "baseline_best_state": _make_baseline_best_state(),
        "snapshot_path": ".txn_txn-001",
        "status": RefactorTransactionStatus.OPEN,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# VAL-REF-001: Creating valid transactions succeeds and round-trips
# ---------------------------------------------------------------------------


class TestValidTransactionCreation:
    def test_open_transaction_round_trips(self) -> None:
        txn = RefactorTransaction(**_valid_kwargs())
        assert txn.transaction_id == "txn-001"
        assert txn.task_id == "task-1"
        assert txn.opening_loop == 5
        assert txn.deadline_loop == 8
        assert txn.declared_regression_keys == ["H-001:0", "H-002:0"]
        assert txn.baseline_accepted_check_keys == ["H-001:0", "H-002:0"]
        assert txn.baseline_accepted_check_count == 2
        assert txn.status == RefactorTransactionStatus.OPEN
        assert txn.snapshot_path == ".txn_txn-001"

        raw = txn.model_dump_json()
        restored = RefactorTransaction.model_validate_json(raw)
        assert restored == txn

    def test_closed_success_transaction_round_trips(self) -> None:
        kwargs = _valid_kwargs(
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="all regressions recovered",
        )
        txn = RefactorTransaction(**kwargs)
        assert txn.status == RefactorTransactionStatus.CLOSED_SUCCESS
        assert txn.closed_loop == 10
        assert txn.close_reason == "all regressions recovered"

        raw = txn.model_dump_json()
        restored = RefactorTransaction.model_validate_json(raw)
        assert restored == txn

    def test_rolled_back_transaction_round_trips(self) -> None:
        kwargs = _valid_kwargs(
            status=RefactorTransactionStatus.ROLLED_BACK,
            closed_loop=9,
            close_reason="unrecovered regressions",
        )
        txn = RefactorTransaction(**kwargs)
        assert txn.status == RefactorTransactionStatus.ROLLED_BACK

        raw = txn.model_dump_json()
        restored = RefactorTransaction.model_validate_json(raw)
        assert restored == txn

    def test_closed_loop_defaults_to_none(self) -> None:
        txn = RefactorTransaction(**_valid_kwargs())
        assert txn.closed_loop is None

    def test_close_reason_defaults_to_none(self) -> None:
        txn = RefactorTransaction(**_valid_kwargs())
        assert txn.close_reason is None

    def test_baseline_best_state_preserves_task_id(self) -> None:
        txn = RefactorTransaction(**_valid_kwargs())
        assert txn.baseline_best_state.task_id == "task-1"


# ---------------------------------------------------------------------------
# VAL-REF-001: Identifier validation failures
# ---------------------------------------------------------------------------


class TestIdentifierValidation:
    def test_empty_transaction_id_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(transaction_id=""))

    def test_empty_task_id_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(task_id=""))

    def test_whitespace_only_transaction_id_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(transaction_id="   "))


# ---------------------------------------------------------------------------
# VAL-REF-001: Declared regression keys validation
# ---------------------------------------------------------------------------


class TestDeclaredRegressionKeys:
    def test_empty_declared_keys_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(declared_regression_keys=[]))

    def test_duplicate_declared_keys_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(
                    declared_regression_keys=["H-001:0", "H-001:0"],
                )
            )


# ---------------------------------------------------------------------------
# VAL-REF-001: Loop and deadline validation
# ---------------------------------------------------------------------------


class TestLoopAndDeadlineValidation:
    def test_negative_opening_loop_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(opening_loop=-1))

    def test_negative_deadline_loop_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(deadline_loop=-1))

    def test_deadline_before_opening_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(opening_loop=5, deadline_loop=4)
            )

    def test_deadline_equal_to_opening_fails(self) -> None:
        """Deadline must be strictly after the opening loop."""
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(opening_loop=5, deadline_loop=5)
            )

    def test_zero_opening_loop_valid(self) -> None:
        txn = RefactorTransaction(
            **_valid_kwargs(opening_loop=0, deadline_loop=3)
        )
        assert txn.opening_loop == 0


# ---------------------------------------------------------------------------
# VAL-REF-001: Baseline payload validation
# ---------------------------------------------------------------------------


class TestBaselinePayloadValidation:
    def test_baseline_count_mismatch_fails(self) -> None:
        """baseline_accepted_check_count must match len(baseline_accepted_check_keys)."""
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(
                    baseline_accepted_check_keys=["H-001:0", "H-002:0"],
                    baseline_accepted_check_count=3,
                )
            )

    def test_empty_baseline_keys_fails(self) -> None:
        """Baseline accepted check keys must be non-empty."""
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(
                    baseline_accepted_check_keys=[],
                    baseline_accepted_check_count=0,
                )
            )

    def test_baseline_best_state_task_mismatch_fails(self) -> None:
        """baseline_best_state.task_id must match task_id."""
        mismatched_state = BestState(
            task_id="different-task",
            state_id="bs-001",
            summary="baseline state",
            accepted_check_keys=["H-001:0", "H-002:0"],
        )
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(baseline_best_state=mismatched_state)
            )


# ---------------------------------------------------------------------------
# VAL-REF-001: Snapshot path validation
# ---------------------------------------------------------------------------


class TestSnapshotPathValidation:
    def test_empty_snapshot_path_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(**_valid_kwargs(snapshot_path=""))

    def test_absolute_snapshot_path_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(snapshot_path="/abs/path/snapshot")
            )

    def test_windows_absolute_snapshot_path_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(snapshot_path="C:\\Users\\snap")
            )

    def test_path_traversal_snapshot_path_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(snapshot_path="../../../etc/passwd")
            )


# ---------------------------------------------------------------------------
# VAL-REF-001: Status validation
# ---------------------------------------------------------------------------


class TestStatusValidation:
    def test_unknown_status_string_fails(self) -> None:
        with pytest.raises(ValidationError):
            RefactorTransaction(
                **_valid_kwargs(status="invalid_status")  # type: ignore[arg-type]
            )

    def test_all_three_statuses_valid(self) -> None:
        for status in RefactorTransactionStatus:
            kwargs = _valid_kwargs(status=status)
            txn = RefactorTransaction(**kwargs)
            assert txn.status == status


# ---------------------------------------------------------------------------
# VAL-REF-001: JSON serialization round-trip preserves all fields
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_json_round_trip_preserves_all_fields(self) -> None:
        txn = RefactorTransaction(**_valid_kwargs())
        raw = txn.model_dump_json()
        data = json.loads(raw)
        assert data["transaction_id"] == "txn-001"
        assert data["task_id"] == "task-1"
        assert data["opening_loop"] == 5
        assert data["deadline_loop"] == 8
        assert data["declared_regression_keys"] == ["H-001:0", "H-002:0"]
        assert data["baseline_accepted_check_keys"] == ["H-001:0", "H-002:0"]
        assert data["baseline_accepted_check_count"] == 2
        assert data["snapshot_path"] == ".txn_txn-001"
        assert data["status"] == "open"

    def test_json_round_trip_with_closed_metadata(self) -> None:
        kwargs = _valid_kwargs(
            status=RefactorTransactionStatus.CLOSED_SUCCESS,
            closed_loop=10,
            close_reason="recovered",
        )
        txn = RefactorTransaction(**kwargs)
        raw = txn.model_dump_json()
        restored = RefactorTransaction.model_validate_json(raw)
        assert restored.closed_loop == 10
        assert restored.close_reason == "recovered"
        assert restored.status == RefactorTransactionStatus.CLOSED_SUCCESS
