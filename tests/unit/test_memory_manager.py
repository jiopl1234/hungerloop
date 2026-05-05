"""Unit tests for MemoryManager + promotion predicates (PRD §19)."""
from __future__ import annotations

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.memory_manager import (
    MemoryManager,
    action_verified,
    non_volatile,
    reusable,
    traceable,
)


def _validation(
    *,
    task_id: str = "t1",
    loop_id: int = 1,
    newly_passed: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    verdict: ValidationVerdict = ValidationVerdict.PASS,
) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        candidate_state_id=f"CAND-{task_id}-{loop_id}",
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed or [],
        evidence_ids=evidence_ids or [],
        has_real_progress=bool(newly_passed),
    )


def _candidate(
    *,
    candidate_id: str = "mem-001",
    task_id: str = "t1",
    content: str = "Verified acceptance check H-001:0",
    evidence_ids: list[str] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate_id,
        task_id=task_id,
        content=content,
        evidence_ids=evidence_ids or [],
    )


# ---- Predicate tests ------------------------------------------------------


def test_action_verified_true_when_overlap() -> None:
    cand = _candidate(evidence_ids=["ev-a", "ev-b"])
    assert action_verified(cand, ["ev-b", "ev-c"]) is True


def test_action_verified_false_when_no_overlap() -> None:
    cand = _candidate(evidence_ids=["ev-a"])
    assert action_verified(cand, ["ev-b", "ev-c"]) is False


def test_action_verified_false_when_either_empty() -> None:
    cand_no_ev = _candidate(evidence_ids=[])
    assert action_verified(cand_no_ev, ["ev-x"]) is False
    cand_with_ev = _candidate(evidence_ids=["ev-a"])
    assert action_verified(cand_with_ev, []) is False


def test_reusable_rejects_task_id_in_content() -> None:
    cand = _candidate(content="Configured task t1 with extra step")
    assert reusable(cand, task_id="t1") is False


def test_reusable_rejects_candidate_id_in_content() -> None:
    cand = _candidate(
        candidate_id="mem-deadbeef",
        content="Saw mem-deadbeef in context",
    )
    assert reusable(cand, task_id="t1") is False


def test_reusable_rejects_loop_token() -> None:
    cand = _candidate(content="Best result reached at loop_007")
    assert reusable(cand, task_id="t1") is False


def test_reusable_accepts_clean_content() -> None:
    cand = _candidate(content="Verified acceptance check H-001:0")
    assert reusable(cand, task_id="t1") is True


def test_non_volatile_threshold_two() -> None:
    repo = InMemoryRepository()
    cand = _candidate(candidate_id="mem-1")
    cand.source_best_state_id = "best-1"
    assert non_volatile(cand, repo) is False
    repo._committed_refs["best-1"] = 1
    assert non_volatile(cand, repo) is False
    repo._committed_refs["best-1"] = 2
    assert non_volatile(cand, repo) is True


def test_non_volatile_does_not_compare_memory_candidate_id() -> None:
    repo = InMemoryRepository()
    cand = _candidate(candidate_id="mem-1")
    repo._committed_refs["mem-1"] = 2
    assert non_volatile(cand, repo) is False


def test_traceable_subset_only() -> None:
    cand = _candidate(evidence_ids=["ev-a", "ev-b"])
    assert traceable(cand, ["ev-a", "ev-b", "ev-c"]) is True
    assert traceable(cand, ["ev-a"]) is False


def test_traceable_false_when_no_candidate_evidence() -> None:
    cand = _candidate(evidence_ids=[])
    assert traceable(cand, ["ev-a"]) is False


# ---- propose_from_loop ----------------------------------------------------


def test_propose_skips_when_no_newly_passed() -> None:
    repo = InMemoryRepository()
    mgr = MemoryManager(repo)
    candidates = mgr.propose_from_loop(
        "t1", 1, _validation(newly_passed=[])
    )
    assert candidates == []
    assert repo.list_memory_candidates("t1") == []


def test_propose_emits_one_candidate_per_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRepository()
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="best-1",
            summary="ok",
            evidence_ids=["ev-1", "ev-2"],
        )
    )
    mgr = MemoryManager(repo)
    candidates = mgr.propose_from_loop(
        "t1",
        2,
        _validation(
            loop_id=2,
            newly_passed=["H-001:0", "H-001:1"],
            evidence_ids=["ev-1"],
        ),
    )
    assert len(candidates) == 2
    saved = repo.list_memory_candidates("t1")
    assert len(saved) == 2
    # action_verified: ev-1 in best.evidence_ids → True
    assert all(c.action_verified for c in candidates)
    # traceable: {"ev-1"} ⊆ {"ev-1","ev-2"} → True
    assert all(c.traceable for c in candidates)
    # reusable: content "Verified acceptance check H-001:0" is task-clean
    assert all(c.reusable for c in candidates)
    # non_volatile: count_committed_references defaults to 0 → False
    assert all(not c.non_volatile for c in candidates)
    assert all(c.source_best_state_id == "best-1" for c in candidates)
    assert all(c.source_candidate_state_id == "CAND-t1-2" for c in candidates)
    assert all(c.source_validation_id == "VAL-t1-2" for c in candidates)


def test_propose_handles_missing_best_state() -> None:
    repo = InMemoryRepository()
    mgr = MemoryManager(repo)
    candidates = mgr.propose_from_loop(
        "t1", 1, _validation(newly_passed=["H-001:0"], evidence_ids=["ev-1"])
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.action_verified is False
    assert cand.traceable is False


# ---- v0.5c.0 lifecycle fields (PRD §19.1) ---------------------------------


def test_emitted_candidate_has_state_proposed() -> None:
    """v0.5c only ever writes ``state="proposed"``; promotion lands later."""
    repo = InMemoryRepository()
    mgr = MemoryManager(repo)
    candidates = mgr.propose_from_loop(
        "t1", 1, _validation(newly_passed=["H-001:0"])
    )
    assert candidates[0].state == "proposed"


def test_emitted_candidate_has_expires_at_90_days_out() -> None:
    """``expires_at`` lands now (pure data) so v0.6's expiry sweep can be
    a pure read-and-flip without a schema migration."""
    from datetime import datetime, timedelta, timezone

    repo = InMemoryRepository()
    mgr = MemoryManager(repo)
    pinned = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    candidates = mgr.propose_from_loop(
        "t1", 1, _validation(newly_passed=["H-001:0"]), now=pinned
    )
    assert candidates[0].expires_at == pinned + timedelta(days=90)


def test_decision_fields_default_null() -> None:
    """``decision_*`` columns are reserved for v0.6 promotion; v0.5c
    leaves them at their default ``None`` / ``""``."""
    repo = InMemoryRepository()
    mgr = MemoryManager(repo)
    [cand] = mgr.propose_from_loop(
        "t1", 1, _validation(newly_passed=["H-001:0"])
    )
    assert cand.decision_loop_id is None
    assert cand.decided_by is None
    assert cand.decision_rationale == ""
    assert cand.replaces_candidate_id is None
