"""Tests for deterministic, score-free commit candidate selection (VAL-REF-005, VAL-REF-006)."""
from __future__ import annotations

import random

from hungerloop.models.enums import AcceptanceCheckType, ValidationVerdict
from hungerloop.models.validation import CheckResult, ValidationReport
from hungerloop.services.commit_selection import (
    CandidateEvaluation,
    evaluation_from_validation_report,
    select_commit_candidate,
    select_fallback_base,
)


def _eval(
    candidate_id: str,
    *,
    newly_passed: list[str] | None = None,
    failing: list[str] | None = None,
    regressed: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    verdict: ValidationVerdict = ValidationVerdict.PASS,
    score: float = 0.0,
) -> CandidateEvaluation:
    """Build a CandidateEvaluation with sensible defaults."""
    return CandidateEvaluation(
        candidate_id=candidate_id,
        newly_passed_check_keys=list(newly_passed or []),
        failing_check_keys=list(failing or []),
        regressed_check_keys=list(regressed or []),
        missing_evidence=list(missing_evidence or []),
        verdict=verdict,
        score=score,
    )


# -----------------------------------------------------------------------
# Gate-passing / gate-failing
# -----------------------------------------------------------------------

class TestGatePassing:
    def test_returns_none_when_all_fail_gate(self) -> None:
        evals = [
            _eval("c1", verdict=ValidationVerdict.FAIL),
            _eval("c2", newly_passed=[], verdict=ValidationVerdict.PASS),
            _eval("c3", newly_passed=["H-001:0"], regressed=["H-002:0"]),
            _eval("c4", newly_passed=["H-001:0"], missing_evidence=["no ev"]),
        ]
        assert select_commit_candidate(evals) is None

    def test_returns_single_gate_passing_candidate(self) -> None:
        evals = [_eval("c1", newly_passed=["H-001:0"])]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c1"

    def test_partial_verdict_with_new_checks_passes_gate(self) -> None:
        evals = [_eval("c1", newly_passed=["H-001:0"], verdict=ValidationVerdict.PARTIAL)]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c1"


# -----------------------------------------------------------------------
# Deterministic ordering
# -----------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_most_newly_passed_wins(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0"]),
            _eval("c2", newly_passed=["H-001:0", "H-002:0", "H-003:0"]),
            _eval("c3", newly_passed=["H-001:0", "H-002:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c2"

    def test_fewest_failing_breaks_tie(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0"], failing=["H-002:0", "H-003:0"]),
            _eval("c2", newly_passed=["H-001:0"], failing=["H-002:0"]),
            _eval("c3", newly_passed=["H-001:0"], failing=["H-002:0", "H-003:0", "H-004:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c2"

    def test_lexicographic_id_breaks_final_tie(self) -> None:
        evals = [
            _eval("ccc", newly_passed=["H-001:0"], failing=["H-002:0"]),
            _eval("aaa", newly_passed=["H-001:0"], failing=["H-002:0"]),
            _eval("bbb", newly_passed=["H-001:0"], failing=["H-002:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "aaa"

    def test_shuffled_input_produces_same_result(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0"], failing=["H-002:0", "H-003:0"]),
            _eval("c2", newly_passed=["H-001:0", "H-002:0"], failing=["H-003:0"]),
            _eval("c3", newly_passed=["H-001:0"], failing=["H-002:0"]),
            _eval("c4", newly_passed=["H-001:0", "H-002:0", "H-003:0"], failing=[]),
            _eval("c5", newly_passed=["H-001:0", "H-002:0"], failing=["H-003:0"]),
        ]
        expected = select_commit_candidate(list(evals))
        # Shuffle many times and verify same result
        rng = random.Random(42)
        for _ in range(50):
            shuffled = list(evals)
            rng.shuffle(shuffled)
            assert select_commit_candidate(shuffled) == expected

    def test_empty_input_returns_none(self) -> None:
        assert select_commit_candidate([]) is None


# -----------------------------------------------------------------------
# Duplicate key handling
# -----------------------------------------------------------------------

class TestDuplicateKeyHandling:
    def test_duplicate_newly_passed_keys_do_not_influence_count(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0", "H-001:0", "H-001:0"]),
            _eval("c2", newly_passed=["H-001:0", "H-002:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c2"

    def test_duplicate_failing_keys_do_not_influence_count(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0"], failing=["H-002:0", "H-002:0", "H-002:0"]),
            _eval("c2", newly_passed=["H-001:0"], failing=["H-002:0", "H-003:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c1"


# -----------------------------------------------------------------------
# Score-free behavior
# -----------------------------------------------------------------------

class TestScoreFree:
    def test_high_score_candidate_does_not_win(self) -> None:
        """Score must never influence selection."""
        evals = [
            _eval("c1", newly_passed=["H-001:0", "H-002:0"], score=999.9),
            _eval("c2", newly_passed=["H-001:0"], score=0.0),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c1"  # wins by more newly passed, not score

    def test_score_does_not_break_ties(self) -> None:
        """Ties must be broken by lexicographic id, not score."""
        evals = [
            _eval("zzz", newly_passed=["H-001:0"], failing=["H-002:0"], score=999.9),
            _eval("aaa", newly_passed=["H-001:0"], failing=["H-002:0"], score=0.0),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "aaa"  # lexicographic wins, not score

    def test_identical_progress_with_different_scores_same_result(self) -> None:
        """Same newly_passed and failing, different scores -> still lexicographic."""
        evals_a = [
            _eval("c1", newly_passed=["H-001:0"], failing=[], score=100.0),
            _eval("c2", newly_passed=["H-001:0"], failing=[], score=0.0),
        ]
        evals_b = [
            _eval("c1", newly_passed=["H-001:0"], failing=[], score=0.0),
            _eval("c2", newly_passed=["H-001:0"], failing=[], score=100.0),
        ]
        # Both should select c1 (lexicographic first), regardless of scores
        assert select_commit_candidate(evals_a)["candidate_id"] == "c1"
        assert select_commit_candidate(evals_b)["candidate_id"] == "c1"


# -----------------------------------------------------------------------
# Mixed gate-passing and gate-failing
# -----------------------------------------------------------------------

class TestMixedGate:
    def test_only_gate_passing_candidates_considered(self) -> None:
        """A failing candidate with more newly passed must not be selected."""
        evals = [
            _eval("c1", newly_passed=["H-001:0"], verdict=ValidationVerdict.PASS),
            _eval(
                "c2",
                newly_passed=["H-001:0", "H-002:0", "H-003:0"],
                verdict=ValidationVerdict.FAIL,
            ),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c1"

    def test_gate_failing_with_regressions_excluded(self) -> None:
        evals = [
            _eval("c1", newly_passed=["H-001:0", "H-002:0"], regressed=["H-003:0"]),
            _eval("c2", newly_passed=["H-001:0"]),
        ]
        result = select_commit_candidate(evals)
        assert result is not None
        assert result["candidate_id"] == "c2"


# -----------------------------------------------------------------------
# ValidationReport adapter
# -----------------------------------------------------------------------

class TestEvaluationFromValidationReport:
    def test_evaluation_from_validation_report(self) -> None:
        report = ValidationReport(
            id="VAL-1",
            task_id="t1",
            loop_id=1,
            candidate_state_id="cand-1",
            baseline_state_id=None,
            verdict=ValidationVerdict.PARTIAL,
            check_results=[
                CheckResult(
                    hunger_item_id="H-1",
                    check_index=0,
                    check_key="H-1:0",
                    check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                    passed=True,
                    detail="ok",
                ),
                CheckResult(
                    hunger_item_id="H-2",
                    check_index=0,
                    check_key="H-2:0",
                    check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                    passed=False,
                    detail="exit=1",
                ),
            ],
            newly_passed_check_keys=["H-1:0"],
            regressed_check_keys=[],
        )
        evaluation = evaluation_from_validation_report("CAND-t1-1-d1", report)
        assert evaluation["candidate_id"] == "CAND-t1-1-d1"
        assert evaluation["newly_passed_check_keys"] == ["H-1:0"]
        assert evaluation["failing_check_keys"] == ["H-2:0"]
        assert evaluation["verdict"] is ValidationVerdict.PARTIAL
        assert evaluation["score"] == 0.0


# -----------------------------------------------------------------------
# Score-free fallback base selection
# -----------------------------------------------------------------------

class TestSelectFallbackBase:
    def test_select_fallback_base_prefers_most_progress_then_fewest_failures(self) -> None:
        def make(
            candidate_id: str, newly: list[str], failing: list[str]
        ) -> CandidateEvaluation:
            return CandidateEvaluation(
                candidate_id=candidate_id,
                newly_passed_check_keys=newly,
                failing_check_keys=failing,
                regressed_check_keys=["H-9:0"],  # gate-failing on purpose
                missing_evidence=[],
                verdict=ValidationVerdict.FAIL,
                score=0.0,
            )

        best = select_fallback_base(
            [
                make("d1", [], ["a", "b", "c"]),
                make("d2", ["x"], ["a", "b", "c"]),
                make("d3", [], ["a"]),
            ]
        )
        assert best is not None and best["candidate_id"] == "d2"
        assert select_fallback_base([]) is None
