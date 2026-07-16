"""Deterministic, score-free commit candidate selection for v0.7 (ADR-010).

``select_commit_candidate`` is a **pure function** that chooses which
candidate to promote from a set of evaluations. It is designed for future
fan-out scenarios where multiple workers produce candidates in the same
loop.

Selection ordering (all deterministic, no score participates):

1. **Gate-passing**: only candidates satisfying strict I-3 are considered.
2. **Most unique newly-passed check keys** (descending).
3. **Fewest unique failing check keys** (ascending).
4. **Lexicographic candidate id** (ascending, stable tie-breaker).

The function never reads ``BestState.score``, ``CandidateState.proposed_score``,
``ValidationReport.score_*``, or any other score field. Score fields on
``CandidateEvaluation`` exist for schema compatibility only and are ignored
by the selection logic (VAL-REF-006).
"""
from __future__ import annotations

from typing import TypedDict

from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import ValidationReport


class CandidateEvaluation(TypedDict):
    """Evaluation of a single candidate for commit selection.

    Fields:
        candidate_id: Unique identifier for the candidate.
        newly_passed_check_keys: Check keys that newly passed this run.
        failing_check_keys: Check keys that failed this run (any failure,
            including regressions).
        regressed_check_keys: Check keys that regressed (were accepted
            in baseline, now failing).
        missing_evidence: Human-readable missing-evidence descriptions.
        verdict: The validation verdict for this candidate.
        score: Schema-only field. **Never read by selection logic.**
            Exists so callers can pass evaluation objects that carry score
            data without stripping it first.
    """

    candidate_id: str
    newly_passed_check_keys: list[str]
    failing_check_keys: list[str]
    regressed_check_keys: list[str]
    missing_evidence: list[str]
    verdict: ValidationVerdict
    score: float


_PASS_VERDICTS: frozenset[ValidationVerdict] = frozenset(
    {ValidationVerdict.PASS, ValidationVerdict.PARTIAL}
)


def _passes_gate(evaluation: CandidateEvaluation) -> bool:
    """Check if an evaluation satisfies strict I-3 gate conditions.

    Strict I-3 requires:
    - Verdict is PASS or PARTIAL
    - At least one newly-passed check key
    - No regressed check keys
    - No missing evidence
    """
    if evaluation["verdict"] not in _PASS_VERDICTS:
        return False
    if not evaluation["newly_passed_check_keys"]:
        return False
    if evaluation["regressed_check_keys"]:
        return False
    if evaluation["missing_evidence"]:
        return False
    return True


def select_commit_candidate(
    evaluations: list[CandidateEvaluation],
) -> CandidateEvaluation | None:
    """Select the best candidate for promotion using deterministic, score-free ordering.

    Args:
        evaluations: List of candidate evaluations in any order.

    Returns:
        The selected ``CandidateEvaluation``, or ``None`` if no evaluation
        passes the strict I-3 gate.

    Ordering (among gate-passing candidates):
        1. Most unique newly-passed check keys (descending).
        2. Fewest unique failing check keys (ascending).
        3. Lexicographic candidate id (ascending).

    This function is pure and deterministic. It does not read any score field.
    Duplicate check keys within an evaluation are de-duplicated before counting
    so they cannot influence selection.
    """
    passing = [ev for ev in evaluations if _passes_gate(ev)]
    if not passing:
        return None

    def sort_key(ev: CandidateEvaluation) -> tuple[int, int, str]:
        unique_newly_passed = len(set(ev["newly_passed_check_keys"]))
        unique_failing = len(set(ev["failing_check_keys"]))
        # Negate newly_passed for descending order; failing and id are ascending
        return (-unique_newly_passed, unique_failing, ev["candidate_id"])

    passing.sort(key=sort_key)
    return passing[0]


def evaluation_from_validation_report(
    candidate_id: str,
    report: ValidationReport,
) -> CandidateEvaluation:
    """Adapt a deterministic ValidationReport into a CandidateEvaluation.

    ``score`` is filled with 0.0 — it is schema-only and never read by
    selection (VAL-REF-006).
    """
    failing = [
        result.check_key for result in report.check_results if not result.passed
    ]
    return CandidateEvaluation(
        candidate_id=candidate_id,
        newly_passed_check_keys=list(report.newly_passed_check_keys),
        failing_check_keys=failing,
        regressed_check_keys=list(report.regressed_check_keys),
        missing_evidence=list(report.missing_evidence),
        verdict=report.verdict,
        score=0.0,
    )


def select_fallback_base(
    evaluations: list[CandidateEvaluation],
) -> CandidateEvaluation | None:
    """Score-free canonical choice when NO evaluation passes the I-3 gate.

    Used by cold-start draft sampling to pick which non-committing draft
    becomes the loop's candidate (and thus the rejected-continuation seed).
    Ordering mirrors ``select_commit_candidate``: most unique newly-passed
    keys, fewest unique failing keys, lexicographic id. Never reads score.
    """
    if not evaluations:
        return None

    def sort_key(ev: CandidateEvaluation) -> tuple[int, int, str]:
        return (
            -len(set(ev["newly_passed_check_keys"])),
            len(set(ev["failing_check_keys"])),
            ev["candidate_id"],
        )

    return sorted(evaluations, key=sort_key)[0]
