"""Tests for deterministic, score-free commit candidate selection (VAL-REF-005, VAL-REF-006)."""
from __future__ import annotations

import random

from hungerloop.models.enums import ValidationVerdict
from hungerloop.services.commit_selection import (
    CandidateEvaluation,
    select_commit_candidate,
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
