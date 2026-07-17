"""Unit tests for the ADR-010 auto-open decision (spreadsheet-04 round-4 fix).

The pure decision function answers: should the orchestrator open a refactor
transaction declaring this loop's regressed keys, so the next continuation
candidate can commit net-positive? Conditions (all required):

- both policy flags on (``refactor_transactions_enabled`` AND
  ``refactor_auto_open_enabled``)
- the rejected candidate regressed a small set of keys (within
  ``max_declared_regressions``)
- every regressed key is a repeat of the previous rejected candidate's
  regressions (fresh breakage is never sanctioned)
- newly-passed count clears both ``refactor_auto_open_min_newly`` and the
  net-positive ratio (3x the regressed count)
"""
from __future__ import annotations

from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.tracing import LoopTrace
from hungerloop.models.validation import ValidationReport
from hungerloop.services.loop_orchestrator import _auto_open_declared_keys


def _policy(**overrides: object) -> HungerPolicy:
    values: dict[str, object] = {
        "refactor_transactions_enabled": True,
        "refactor_auto_open_enabled": True,
        "refactor_auto_open_min_newly": 5,
        "max_declared_regressions": 3,
    }
    values.update(overrides)
    return HungerPolicy.model_validate(values)


def _report(newly: int, regressed: list[str]) -> ValidationReport:
    return ValidationReport(
        id="VAL-t1-6",
        task_id="t1",
        loop_id=6,
        candidate_state_id="CAND-t1-6",
        baseline_state_id=None,
        verdict=ValidationVerdict.FAIL,
        attempted_hunger_item_ids=["H-001"],
        newly_passed_check_keys=[f"H-001:{index}" for index in range(100, 100 + newly)],
        regressed_check_keys=regressed,
        has_real_progress=False,
    )


def _prior(regressed: list[str], *, committed: bool = False) -> LoopTrace:
    return LoopTrace(
        task_id="t1",
        loop_id=4,
        phase="work",
        active_hunger=1.0,
        drive_budget=1.0,
        work_pressure=1.0,
        candidate_state_id="CAND-t1-4",
        committed=committed,
        regressed_check_keys=regressed,
    )


def test_opens_on_repeated_single_key_with_large_newly() -> None:
    keys = _auto_open_declared_keys(
        _policy(), _report(17, ["H-001:10"]), _prior(["H-001:10"])
    )
    assert keys == ["H-001:10"]


def test_none_when_either_flag_off() -> None:
    report = _report(17, ["H-001:10"])
    prior = _prior(["H-001:10"])
    assert (
        _auto_open_declared_keys(
            _policy(refactor_auto_open_enabled=False), report, prior
        )
        is None
    )
    assert (
        _auto_open_declared_keys(
            _policy(refactor_transactions_enabled=False), report, prior
        )
        is None
    )


def test_none_without_regressions_or_prior_trace() -> None:
    assert _auto_open_declared_keys(_policy(), _report(17, []), _prior(["k"])) is None
    assert _auto_open_declared_keys(_policy(), _report(17, ["H-001:10"]), None) is None


def test_none_when_prior_candidate_committed() -> None:
    prior = _prior(["H-001:10"], committed=True)
    assert _auto_open_declared_keys(_policy(), _report(17, ["H-001:10"]), prior) is None


def test_none_when_regression_is_fresh() -> None:
    # Current regresses a key the previous rejection did not: fresh breakage.
    keys = _auto_open_declared_keys(
        _policy(), _report(17, ["H-001:10", "H-001:12"]), _prior(["H-001:10"])
    )
    assert keys is None


def test_none_below_min_newly() -> None:
    keys = _auto_open_declared_keys(
        _policy(), _report(4, ["H-001:10"]), _prior(["H-001:10"])
    )
    assert keys is None


def test_none_below_net_positive_ratio() -> None:
    # Two repeated regressions need 3x2=6 newly-passed; 5 clears min_newly
    # but not the ratio.
    regressed = ["H-001:10", "H-001:11"]
    keys = _auto_open_declared_keys(
        _policy(), _report(5, regressed), _prior(regressed)
    )
    assert keys is None
    keys = _auto_open_declared_keys(
        _policy(), _report(6, regressed), _prior(regressed)
    )
    assert keys == regressed


def test_none_when_too_many_regressed_keys() -> None:
    regressed = [f"H-001:{index}" for index in range(10, 14)]  # 4 > max 3
    keys = _auto_open_declared_keys(
        _policy(), _report(20, regressed), _prior(regressed)
    )
    assert keys is None
