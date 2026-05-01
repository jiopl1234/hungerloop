from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import CheckResult, ValidationReport


def _check_result(
    check_key: str,
    passed: bool,
    previously_passed: bool = False,
) -> CheckResult:
    item_id, idx_str = check_key.split(":", 1)
    return CheckResult(
        hunger_item_id=item_id,
        check_index=int(idx_str),
        check_key=check_key,
        check_type="file_exists",
        passed=passed,
        previously_passed=previously_passed,
        newly_passed=passed and not previously_passed,
        regressed=previously_passed and not passed,
        detail=f"check {check_key}: {passed}",
    )


def test_single_item_first_check_passes_is_progress() -> None:
    report = ValidationReport(
        id="VAL-t1-1",
        task_id="t1",
        loop_id=1,
        candidate_state_id="CAND-t1-1",
        baseline_state_id=None,
        verdict=ValidationVerdict.PARTIAL,
        attempted_hunger_item_ids=["H-001"],
        check_results=[
            _check_result("H-001:0", passed=True),
            _check_result("H-001:1", passed=False),
        ],
        currently_passed_check_keys=["H-001:0"],
        newly_passed_check_keys=["H-001:0"],
        regressed_check_keys=[],
        satisfied_hunger_item_ids=[],
        unsatisfied_hunger_item_ids=["H-001"],
        evidence_ids=["ev-1"],
        has_real_progress=True,
    )
    assert report.has_real_progress is True
    assert report.newly_passed_check_keys == ["H-001:0"]


def test_second_check_passes_later_is_also_progress() -> None:
    report = ValidationReport(
        id="VAL-t1-2",
        task_id="t1",
        loop_id=2,
        candidate_state_id="CAND-t1-2",
        baseline_state_id="CAND-t1-1",
        verdict=ValidationVerdict.PASS,
        attempted_hunger_item_ids=["H-001"],
        check_results=[
            _check_result("H-001:0", passed=True, previously_passed=True),
            _check_result("H-001:1", passed=True, previously_passed=False),
        ],
        currently_passed_check_keys=["H-001:0", "H-001:1"],
        newly_passed_check_keys=["H-001:1"],
        regressed_check_keys=[],
        satisfied_hunger_item_ids=["H-001"],
        unsatisfied_hunger_item_ids=[],
        evidence_ids=["ev-2"],
        has_real_progress=True,
    )
    assert report.has_real_progress is True


def test_no_new_checks_is_no_progress() -> None:
    report = ValidationReport(
        id="VAL-t1-3",
        task_id="t1",
        loop_id=3,
        candidate_state_id="CAND-t1-3",
        baseline_state_id="CAND-t1-2",
        verdict=ValidationVerdict.FAIL,
        attempted_hunger_item_ids=["H-001"],
        check_results=[
            _check_result("H-001:0", passed=False),
        ],
        currently_passed_check_keys=[],
        newly_passed_check_keys=[],
        regressed_check_keys=[],
        satisfied_hunger_item_ids=[],
        unsatisfied_hunger_item_ids=["H-001"],
        evidence_ids=["ev-3"],
        has_real_progress=False,
    )
    assert report.has_real_progress is False
