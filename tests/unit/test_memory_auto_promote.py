"""Unit tests for memory auto-promote (VAL-MEM-001 through VAL-MEM-021).

Covers:
- VAL-MEM-001: Memory candidates contain reusable check insights
- VAL-MEM-002: Candidate generation preserves check-level metadata
- VAL-MEM-003: Auto-promotion is policy gated
- VAL-MEM-004: Auto-promotion requires every existing predicate
- VAL-MEM-005: Auto-promotion is durable and idempotent
- VAL-MEM-006: Promoted-memory repository APIs support cross-task reuse
- VAL-MEM-007: DONE stop-report persistence precedes auto-promotion
- VAL-MEM-008: Memory policy defaults preserve v0.6 compatibility
- VAL-MEM-017: Memory evidence digests are deterministic and bounded
- VAL-MEM-018: Auto-promotion writes are transactional
- VAL-MEM-020: Unresolved memory check keys fail closed
- VAL-MEM-021: Auto-promotion respects candidate lifecycle and expiry
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    EvidenceType,
    StopReason,
    ValidationVerdict,
)
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.memory import MemoryCandidate, PromotedMemory
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation import ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.memory_manager import MemoryManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _setup_ledger(
    repo: InMemoryRepository,
    *,
    task_id: str = "t1",
    items: list[tuple[str, str, list[AcceptanceCheck]]] | None = None,
) -> HungerLedger:
    """Create a hunger ledger with the given items.

    Each tuple is (item_id, title, acceptance_checks).
    """
    if items is None:
        items = [
            (
                "H-001",
                "Implement core feature",
                [
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                        params={"argv": ["python", "-m", "pytest"]},
                        description="All unit tests pass",
                    ),
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.FILE_EXISTS,
                        params={"path": "src/main.py"},
                        description="Main module exists",
                    ),
                ],
            ),
            (
                "H-002",
                "Add error handling",
                [
                    AcceptanceCheck(
                        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                        params={"argv": ["python", "-c", "import main"]},
                        description="Import succeeds without error",
                    ),
                ],
            ),
        ]
    hunger_items = [
        HungerItem(id=item_id, title=title, acceptance_checks=checks)
        for item_id, title, checks in items
    ]
    ledger = HungerLedger(task_id=task_id, items=hunger_items)
    repo.save_hunger_ledger(task_id, ledger)
    return ledger


def _validation(
    *,
    task_id: str = "t1",
    loop_id: int = 1,
    newly_passed: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    verdict: ValidationVerdict = ValidationVerdict.PASS,
    candidate_state_id: str = "CAND-t1-1",
) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        candidate_state_id=candidate_state_id,
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed or [],
        evidence_ids=evidence_ids or [],
        has_real_progress=bool(newly_passed),
    )


def _seed_done_stop_report(
    repo: InMemoryRepository,
    *,
    task_id: str = "t1",
    best_state_id: str = "best-1",
) -> StopReport:
    report = StopReport(
        task_id=task_id,
        stop_reason=StopReason.DONE,
        goal_status="completed",
        final_best_state_id=best_state_id,
    )
    repo.save_stop_report(report)
    return report


def _seed_best_state(
    repo: InMemoryRepository,
    *,
    task_id: str = "t1",
    state_id: str = "best-1",
    evidence_ids: list[str] | None = None,
    accepted_check_keys: list[str] | None = None,
) -> BestState:
    best = BestState(
        task_id=task_id,
        state_id=state_id,
        summary="ok",
        evidence_ids=evidence_ids or ["ev-1"],
        accepted_check_keys=accepted_check_keys or [],
    )
    repo.save_best_state(best)
    return best


# ---------------------------------------------------------------------------
# VAL-MEM-001: Memory candidates contain reusable check insights
# ---------------------------------------------------------------------------


class TestMemoryCandidateContent:
    """VAL-MEM-001: Memory candidates contain reusable check insights."""

    def test_candidate_content_contains_check_key_item_title_and_description(
        self,
    ) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=["ev-1"]),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # Must contain the check key
        assert "H-001:0" in content
        # Must contain the item title
        assert "Implement core feature" in content
        # Must contain the check description
        assert "All unit tests pass" in content
        # Must NOT be the old fallback format
        assert content != "Verified acceptance check H-001:0"

    def test_candidate_content_omits_volatile_identifiers(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=["ev-1"]),
        )
        content = candidates[0].content
        # Must not contain workspace paths, task ids, candidate ids, etc.
        assert "CAND-" not in content
        assert "VAL-" not in content
        assert "candidates/loop_" not in content
        assert "best/" not in content
        assert "/tmp/" not in content
        assert "task_" not in content.lower() or "task" not in content.lower()

    def test_candidate_content_omits_secrets_and_env_values(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=["ev-1"]),
        )
        content = candidates[0].content
        assert "API_KEY" not in content
        assert "Bearer" not in content
        assert ".env" not in content
        # No raw secret values
        assert "sk-" not in content

    def test_evidence_digest_included_when_prompt_safe_evidence_available(
        self,
    ) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Save a successful tool call evidence and capture the real evidence id
        ev_id = repo.save_tool_call_as_evidence(
            task_id="t1",
            loop_id=1,
            agent_id="worker-1",
            tool_name="run_shell",
            args_summary="pytest",
            result_summary="2 passed",
            success=True,
            elapsed_ms=100,
        )
        _seed_best_state(repo, evidence_ids=[ev_id])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_id]),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # The content should include the evidence digest with tool name and result
        assert "run_shell" in content
        assert "2 passed" in content

    def test_evidence_digest_omitted_when_no_prompt_safe_evidence(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=[])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[]),
        )
        content = candidates[0].content
        # When no prompt-safe evidence is available, digest is omitted
        # Content should still contain check key, title, and description
        assert "H-001:0" in content
        assert "Implement core feature" in content
        assert "All unit tests pass" in content
        # Should not contain evidence-related volatile content
        assert "CAND-" not in content


# ---------------------------------------------------------------------------
# VAL-MEM-002: Candidate generation preserves check-level metadata
# ---------------------------------------------------------------------------


class TestCandidateMetadataPreservation:
    """VAL-MEM-002: Candidate generation preserves check-level metadata."""

    def test_one_candidate_per_resolvable_unique_key(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1", "ev-2"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            2,
            _validation(
                loop_id=2,
                newly_passed=["H-001:0", "H-001:1", "H-002:0"],
                evidence_ids=["ev-1"],
            ),
        )
        assert len(candidates) == 3
        saved = repo.list_memory_candidates("t1")
        assert len(saved) == 3

    def test_no_candidates_when_no_newly_passed_checks(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1", 1, _validation(newly_passed=[])
        )
        assert candidates == []
        assert repo.list_memory_candidates("t1") == []

    def test_preserves_evidence_ids_and_check_keys(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1", "ev-2"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            2,
            _validation(
                loop_id=2,
                newly_passed=["H-001:0", "H-002:0"],
                evidence_ids=["ev-1", "ev-2"],
                candidate_state_id="CAND-t1-2",
            ),
        )
        assert len(candidates) == 2
        for c in candidates:
            assert c.evidence_ids == ["ev-1", "ev-2"]
            assert c.source_loop_ids == [2]
            assert c.source_candidate_state_id == "CAND-t1-2"
            assert c.source_validation_id == "VAL-t1-2"
            # VAL-MEM-002: referenced and accepted check keys are preserved
            assert c.referenced_check_keys == ["H-001:0", "H-002:0"]
            assert c.accepted_check_keys == ["H-001:0", "H-002:0"]

    def test_preserves_source_best_state_id(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, state_id="best-42", evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=["ev-1"]),
        )
        assert len(candidates) == 1
        assert candidates[0].source_best_state_id == "best-42"

    def test_preserves_expires_at(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        pinned = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"]),
            now=pinned,
        )
        assert candidates[0].expires_at == pinned + timedelta(days=90)


# ---------------------------------------------------------------------------
# VAL-MEM-020: Unresolved memory check keys fail closed
# ---------------------------------------------------------------------------


class TestUnresolvedCheckKeys:
    """VAL-MEM-020: Unresolved memory check keys fail closed."""

    def test_missing_key_produces_no_candidate(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-999:0"]),  # H-999 doesn't exist
        )
        assert candidates == []

    def test_ambiguous_key_produces_no_candidate(self) -> None:
        """A key with an out-of-range check index is unresolved."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        # H-001 has 2 checks (index 0 and 1); index 5 is out of range
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:5"]),
        )
        assert candidates == []

    def test_malformed_key_produces_no_candidate(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        # No colon separator
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001"]),
        )
        assert candidates == []

    def test_duplicate_keys_in_report_produce_one_candidate(self) -> None:
        """Duplicate newly_passed_check_keys in one report produce one candidate."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(
                newly_passed=["H-001:0", "H-001:0"],
                evidence_ids=["ev-1"],
            ),
        )
        assert len(candidates) == 1

    def test_valid_and_invalid_keys_produce_only_valid_candidates(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=["ev-1"])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(
                newly_passed=["H-001:0", "H-999:0", "H-001:5"],
                evidence_ids=["ev-1"],
            ),
        )
        assert len(candidates) == 1
        assert "H-001:0" in candidates[0].content

    def test_no_fallback_content_for_unresolvable_keys(self) -> None:
        """Unresolvable keys never produce 'Verified acceptance check' fallback."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-999:0"]),
        )
        assert candidates == []
        saved = repo.list_memory_candidates("t1")
        assert saved == []


# ---------------------------------------------------------------------------
# VAL-MEM-017: Memory evidence digests are deterministic and bounded
# ---------------------------------------------------------------------------


class TestEvidenceDigest:
    """VAL-MEM-017: Memory evidence digests are deterministic and bounded."""

    def test_digest_uses_only_successful_prompt_safe_evidence(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Save successful tool call evidence and capture the real evidence id
        ev_success = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": "All tests passed",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        # Save failed tool call evidence and capture the real evidence id
        ev_fail = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": "Error occurred",
                "success": False,
                "agent_id": "worker-1",
                "elapsed_ms": 50,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_success, ev_fail])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(
                newly_passed=["H-001:0"],
                evidence_ids=[ev_success, ev_fail],
            ),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # The digest should include the successful evidence
        assert "All tests passed" in content
        # The digest should not include "Error occurred" from failed evidence
        assert "Error occurred" not in content

    def test_digest_is_bounded_in_length(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Save evidence with very long output and capture the real evidence id
        long_output = "x" * 10000
        ev_id = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": long_output,
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_id])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_id]),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # Content should be bounded - not contain 10000 chars of 'x'
        assert len(content) < 5000

    def test_digest_omitted_when_no_safe_evidence_available(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(repo, evidence_ids=[])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[]),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # When no safe evidence, content is still valid but without digest
        assert "H-001:0" in content
        assert "Implement core feature" in content

    def test_digest_excludes_volatile_paths_and_secrets(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Save evidence with volatile paths and capture the real evidence id
        ev_id = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": "Test run from /tmp/workspace/tasks/t1/candidates/loop_001",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_id])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_id]),
        )
        content = candidates[0].content
        # Volatile paths must not leak into digest
        assert "/tmp/workspace" not in content
        assert "candidates/loop_" not in content

    def test_digest_excludes_windows_absolute_paths(self) -> None:
        """VAL-MEM-001/017: Windows absolute paths must not leak into content."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        ev_id = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": "Output written to C:\\Users\\test\\output.txt",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_id])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_id]),
        )
        content = candidates[0].content
        # Windows absolute path must not leak
        assert "C:\\" not in content
        assert "C:/" not in content

    def test_digest_excludes_posix_absolute_paths(self) -> None:
        """VAL-MEM-001/017: POSIX absolute paths must not leak into content."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        ev_id = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "run_shell",
                "result_summary": "Config loaded from /home/user/config.yaml",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_id])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_id]),
        )
        content = candidates[0].content
        # POSIX absolute path must not leak
        assert "/home/user" not in content

    def test_digest_deterministic_ordering_with_multiple_evidence(self) -> None:
        """VAL-MEM-017: Digests preserve stable ordering by content."""
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Save two successful tool call evidence items in non-sorted order
        ev_z = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "zebra_tool",
                "result_summary": "zebra result",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        ev_a = repo.save_evidence(
            task_id="t1",
            loop_id=1,
            evidence_type=EvidenceType.TOOL_CALL,
            payload={
                "tool_name": "alpha_tool",
                "result_summary": "alpha result",
                "success": True,
                "agent_id": "worker-1",
                "elapsed_ms": 100,
            },
        )
        _seed_best_state(repo, evidence_ids=[ev_z, ev_a])

        mgr = MemoryManager(repo)
        candidates = mgr.propose_from_loop(
            "t1",
            1,
            _validation(newly_passed=["H-001:0"], evidence_ids=[ev_z, ev_a]),
        )
        assert len(candidates) == 1
        content = candidates[0].content
        # alpha_tool should appear before zebra_tool (sorted order)
        alpha_pos = content.find("alpha_tool")
        zebra_pos = content.find("zebra_tool")
        assert alpha_pos != -1
        assert zebra_pos != -1
        assert alpha_pos < zebra_pos





class TestAutoPromotePolicyGated:
    """VAL-MEM-003: Auto-promotion is policy gated."""

    def _setup_promotable_candidate(
        self,
        repo: InMemoryRepository,
        *,
        task_id: str = "t1",
        candidate_id: str = "mem-001",
        best_state_id: str = "best-1",
    ) -> MemoryCandidate:
        repo.create_task(task_id, "Goal")
        _setup_ledger(repo, task_id=task_id)
        _seed_best_state(
            repo,
            task_id=task_id,
            state_id=best_state_id,
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(
            repo, task_id=task_id, best_state_id=best_state_id
        )
        cand = MemoryCandidate(
            candidate_id=candidate_id,
            task_id=task_id,
            content="Check H-001:0: Implement core feature - All unit tests pass",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id=best_state_id,
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)
        return cand

    def test_disabled_policy_produces_no_promotions(self) -> None:
        repo = InMemoryRepository()
        self._setup_promotable_candidate(repo)

        policy = repo.get_hunger_policy("t1")
        policy.memory_auto_promote_enabled = False
        repo.set_hunger_policy("t1", policy)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert result == []
        promoted = repo.list_promoted_memories("t1")
        assert promoted == []
        # Candidate state should be unchanged
        cand = repo.get_memory_candidate("mem-001")
        assert cand is not None
        assert cand.state == "proposed"
        assert cand.decided_by is None

    def test_enabled_policy_promotes_eligible_candidate(self) -> None:
        repo = InMemoryRepository()
        self._setup_promotable_candidate(repo)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert len(result) == 1
        promoted = repo.list_promoted_memories("t1")
        assert len(promoted) == 1


# ---------------------------------------------------------------------------
# VAL-MEM-004: Auto-promotion requires every existing predicate
# ---------------------------------------------------------------------------


class TestAutoPromotePredicates:
    """VAL-MEM-004: Auto-promotion requires every existing predicate."""

    def _setup_candidate_with_predicates(
        self,
        repo: InMemoryRepository,
        *,
        action_verified_false: bool = False,
        reusable_false: bool = False,
        non_volatile_false: bool = False,
        traceable_false: bool = False,
        candidate_id: str = "mem-001",
        stop_reason: StopReason = StopReason.DONE,
        best_state_id: str = "best-1",
    ) -> MemoryCandidate:
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)

        # Set up best state evidence. When we want action_verified=False,
        # we make evidence_ids not overlap with best evidence_ids.
        if action_verified_false:
            best_ev_ids: list[str] = ["ev-best-1"]
            cand_ev_ids: list[str] = ["ev-cand-1"]
        else:
            best_ev_ids = ["ev-1"]
            cand_ev_ids = ["ev-1"]

        # When we want traceable=False, candidate evidence is not a subset of best.
        if traceable_false:
            best_ev_ids = ["ev-best-1"]
            cand_ev_ids = ["ev-cand-1", "ev-cand-2"]
            # action_verified will also be false, but that's fine for the test

        _seed_best_state(
            repo,
            state_id=best_state_id,
            evidence_ids=best_ev_ids,
            accepted_check_keys=["H-001:0"],
        )
        report = StopReport(
            task_id="t1",
            stop_reason=stop_reason,
            goal_status="completed" if stop_reason == StopReason.DONE else "abandoned",
            final_best_state_id=best_state_id,
        )
        repo.save_stop_report(report)

        # For non_volatile=False, set source_best_state_id to a different id
        # than the final best state id.
        source_best = best_state_id
        if non_volatile_false:
            source_best = "best-other"

        # For reusable=False, include task-specific content.
        content = "Check H-001:0: Implement core feature - All unit tests pass"
        if reusable_false:
            content = "Check H-001:0 task_550e8400-e29b-41d4-a716-446655440000"

        cand = MemoryCandidate(
            candidate_id=candidate_id,
            task_id="t1",
            content=content,
            evidence_ids=cand_ev_ids,
            accepted_check_keys=["H-001:0"],
            source_best_state_id=source_best,
            source_loop_ids=[1],
            state="proposed",
            action_verified=not action_verified_false,
            reusable=not reusable_false,
            non_volatile=not non_volatile_false,
            traceable=not traceable_false,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)
        return cand

    def test_action_verified_false_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(repo, action_verified_false=True)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_reusable_false_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(repo, reusable_false=True)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_non_volatile_false_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(repo, non_volatile_false=True)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_traceable_false_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(repo, traceable_false=True)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_non_done_stop_reason_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(
            repo, stop_reason=StopReason.HUNGER_EXPIRED
        )
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_mismatched_best_state_id_skips_promotion(self) -> None:
        repo = InMemoryRepository()
        # Candidate references best-1, but stop report says best-2
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-2", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        report = StopReport(
            task_id="t1",
            stop_reason=StopReason.DONE,
            goal_status="completed",
            final_best_state_id="best-2",
        )
        repo.save_stop_report(report)
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",  # Mismatch!
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_all_predicates_true_promotes(self) -> None:
        repo = InMemoryRepository()
        self._setup_candidate_with_predicates(repo)
        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# VAL-MEM-005: Auto-promotion is durable and idempotent
# ---------------------------------------------------------------------------


class TestAutoPromoteIdempotent:
    """VAL-MEM-005: Auto-promotion is durable and idempotent."""

    def test_promoted_candidate_has_correct_fields(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0: Implement core feature - All unit tests pass",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert len(result) == 1
        promoted = result[0]
        assert promoted.source_candidate_id == "mem-001"
        assert promoted.task_id == "t1"
        assert promoted.approved_by == "auto"
        assert promoted.layer == "task"
        assert promoted.evidence_ids == ["ev-1"]
        assert promoted.accepted_check_keys == ["H-001:0"]
        assert promoted.created_at is not None

    def test_source_candidate_marked_approved_with_auto_review(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        mgr.auto_promote("t1")

        updated = repo.get_memory_candidate("mem-001")
        assert updated is not None
        assert updated.state == "approved"
        assert updated.status == "approved"
        assert updated.decided_by == "auto"
        assert updated.reviewer == "auto"
        assert updated.reviewed_at is not None

    def test_rerun_does_not_create_duplicate_promotions(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        result1 = mgr.auto_promote("t1")
        assert len(result1) == 1

        result2 = mgr.auto_promote("t1")
        assert result2 == []

        promoted = repo.list_promoted_memories("t1")
        assert len(promoted) == 1


# ---------------------------------------------------------------------------
# VAL-MEM-006: Promoted-memory repository APIs support cross-task reuse
# ---------------------------------------------------------------------------


class TestPromotedMemoryRepository:
    """VAL-MEM-006: Promoted-memory repository APIs support cross-task reuse."""

    def test_in_memory_save_get_list_promoted_memories(self) -> None:
        repo = InMemoryRepository()
        now = datetime.now(timezone.utc)
        m1 = PromotedMemory(
            memory_id="prom-1",
            source_candidate_id="mem-1",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            created_at=now,
            approved_by="auto",
        )
        m2 = PromotedMemory(
            memory_id="prom-2",
            source_candidate_id="mem-2",
            task_id="t2",
            content="Check H-002:0",
            evidence_ids=["ev-2"],
            accepted_check_keys=["H-002:0"],
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(m1)
        repo.save_promoted_memory(m2)

        # Get single
        assert repo.get_promoted_memory("prom-1") is not None
        assert repo.get_promoted_memory("nonexistent") is None

        # List all tasks
        all_promoted = repo.list_promoted_memories()
        assert len(all_promoted) == 2

        # List by task
        t1_promoted = repo.list_promoted_memories("t1")
        assert len(t1_promoted) == 1
        assert t1_promoted[0].memory_id == "prom-1"

        t2_promoted = repo.list_promoted_memories("t2")
        assert len(t2_promoted) == 1
        assert t2_promoted[0].memory_id == "prom-2"

    def test_sqlite_save_get_list_promoted_memories_and_reopen(
        self, tmp_path: object,
    ) -> None:
        from pathlib import Path

        from hungerloop.repository.sqlite_repo import SQLiteRepository

        db_path = Path(str(tmp_path)) / "test_mem.db"
        repo = SQLiteRepository(db_path)
        repo.create_task("t1", "Goal")
        repo.create_task("t2", "Goal")
        now = datetime.now(timezone.utc)

        # Need to save memory candidates first due to FK constraint
        c1 = MemoryCandidate(
            candidate_id="mem-1",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        c2 = MemoryCandidate(
            candidate_id="mem-2",
            task_id="t2",
            content="Check H-002:0",
            evidence_ids=["ev-2"],
            accepted_check_keys=["H-002:0"],
        )
        repo.save_memory_candidate(c1)
        repo.save_memory_candidate(c2)

        m1 = PromotedMemory(
            memory_id="prom-1",
            source_candidate_id="mem-1",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            created_at=now,
            approved_by="auto",
        )
        m2 = PromotedMemory(
            memory_id="prom-2",
            source_candidate_id="mem-2",
            task_id="t2",
            content="Check H-002:0",
            evidence_ids=["ev-2"],
            accepted_check_keys=["H-002:0"],
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(m1)
        repo.save_promoted_memory(m2)

        # Get single
        assert repo.get_promoted_memory("prom-1") is not None

        # List all
        assert len(repo.list_promoted_memories()) == 2
        # List by task
        assert len(repo.list_promoted_memories("t1")) == 1

        # Reopen and verify persistence
        repo.close()
        repo2 = SQLiteRepository(db_path)
        all_promoted = repo2.list_promoted_memories()
        assert len(all_promoted) == 2
        m1_loaded = repo2.get_promoted_memory("prom-1")
        assert m1_loaded is not None
        assert m1_loaded.evidence_ids == ["ev-1"]
        assert m1_loaded.accepted_check_keys == ["H-001:0"]
        assert m1_loaded.approved_by == "auto"
        repo2.close()

    def test_source_candidate_uniqueness_in_memory(self) -> None:
        repo = InMemoryRepository()
        now = datetime.now(timezone.utc)
        m1 = PromotedMemory(
            memory_id="prom-1",
            source_candidate_id="mem-1",
            task_id="t1",
            content="Check H-001:0",
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(m1)

        # Saving a second promoted memory with the same source_candidate_id
        # should not create a duplicate
        m2 = PromotedMemory(
            memory_id="prom-2",
            source_candidate_id="mem-1",  # Same source!
            task_id="t1",
            content="Check H-001:0",
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(m2)

        promoted = repo.list_promoted_memories("t1")
        # Should have only one promoted memory for source mem-1
        source_ids = [p.source_candidate_id for p in promoted]
        assert source_ids.count("mem-1") == 1


# ---------------------------------------------------------------------------
# VAL-MEM-007: DONE stop-report persistence precedes auto-promotion
# ---------------------------------------------------------------------------


class TestStopReportPrecedesPromotion:
    """VAL-MEM-007: DONE stop-report persistence precedes auto-promotion."""

    def test_no_auto_promotion_without_done_stop_report(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        # No stop report saved
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert result == []
        assert repo.list_promoted_memories("t1") == []

    def test_non_done_stop_report_does_not_trigger_promotion(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        report = StopReport(
            task_id="t1",
            stop_reason=StopReason.BLOCKED,
            goal_status="blocked",
            final_best_state_id="best-1",
        )
        repo.save_stop_report(report)
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert result == []

    def test_done_stop_report_allows_auto_promotion(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# VAL-MEM-008: Memory policy defaults preserve v0.6 compatibility
# ---------------------------------------------------------------------------


class TestMemoryPolicyDefaults:
    """VAL-MEM-008: Memory policy defaults preserve v0.6 compatibility."""

    def test_default_policy_has_auto_promote_enabled(self) -> None:
        policy = HungerPolicy()
        assert policy.memory_auto_promote_enabled is True

    def test_default_policy_has_recall_enabled(self) -> None:
        policy = HungerPolicy()
        assert policy.memory_recall_enabled is True

    def test_policy_round_trips_through_in_memory_repo(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        policy = HungerPolicy(
            memory_auto_promote_enabled=False,
            memory_recall_enabled=False,
        )
        repo.set_hunger_policy("t1", policy)
        loaded = repo.get_hunger_policy("t1")
        assert loaded.memory_auto_promote_enabled is False
        assert loaded.memory_recall_enabled is False

    def test_policy_round_trips_through_sqlite_repo(
        self, tmp_path: object,
    ) -> None:
        from pathlib import Path

        from hungerloop.repository.sqlite_repo import SQLiteRepository

        db_path = Path(str(tmp_path)) / "test_policy.db"
        repo = SQLiteRepository(db_path)
        repo.create_task("t1", "Goal")
        policy = HungerPolicy(
            memory_auto_promote_enabled=False,
            memory_recall_enabled=False,
        )
        repo.set_hunger_policy("t1", policy)
        loaded = repo.get_hunger_policy("t1")
        assert loaded.memory_auto_promote_enabled is False
        assert loaded.memory_recall_enabled is False
        repo.close()

    def test_explicit_true_values_round_trip(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        policy = HungerPolicy(
            memory_auto_promote_enabled=True,
            memory_recall_enabled=True,
        )
        repo.set_hunger_policy("t1", policy)
        loaded = repo.get_hunger_policy("t1")
        assert loaded.memory_auto_promote_enabled is True
        assert loaded.memory_recall_enabled is True


# ---------------------------------------------------------------------------
# VAL-MEM-018: Auto-promotion writes are transactional
# ---------------------------------------------------------------------------


class TestAutoPromoteTransactional:
    """VAL-MEM-018: Auto-promotion writes are transactional."""

    def test_promotion_writes_candidate_state_and_promoted_memory_and_event(
        self,
    ) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        mgr.auto_promote("t1")

        # All three writes should be present
        updated = repo.get_memory_candidate("mem-001")
        assert updated is not None
        assert updated.state == "approved"
        assert updated.decided_by == "auto"

        promoted = repo.list_promoted_memories("t1")
        assert len(promoted) == 1

        events = repo.list_events("t1", event_types=["memory_promoted"])
        assert len(events) == 1

    def test_idempotent_retry_after_success(self) -> None:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        cand = MemoryCandidate(
            candidate_id="mem-001",
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state="proposed",
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        repo.save_memory_candidate(cand)

        mgr = MemoryManager(repo)
        # First promotion
        mgr.auto_promote("t1")
        # Retry
        mgr.auto_promote("t1")

        # Only one promoted memory, one event
        promoted = repo.list_promoted_memories("t1")
        assert len(promoted) == 1
        events = repo.list_events("t1", event_types=["memory_promoted"])
        assert len(events) == 1


# ---------------------------------------------------------------------------
# VAL-MEM-021: Auto-promotion respects candidate lifecycle and expiry
# ---------------------------------------------------------------------------


class TestAutoPromoteLifecycle:
    """VAL-MEM-021: Auto-promotion respects candidate lifecycle and expiry."""

    def _make_candidate(
        self,
        *,
        state: str = "proposed",
        expires_at: datetime | None = None,
        candidate_id: str = "mem-001",
    ) -> MemoryCandidate:
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        return MemoryCandidate(
            candidate_id=candidate_id,
            task_id="t1",
            content="Check H-001:0",
            evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
            source_best_state_id="best-1",
            source_loop_ids=[1],
            state=state,  # type: ignore[arg-type]
            action_verified=True,
            reusable=True,
            non_volatile=True,
            traceable=True,
            expires_at=expires_at,
        )

    def _setup_repo(self) -> InMemoryRepository:
        repo = InMemoryRepository()
        repo.create_task("t1", "Goal")
        _setup_ledger(repo)
        _seed_best_state(
            repo, state_id="best-1", evidence_ids=["ev-1"],
            accepted_check_keys=["H-001:0"],
        )
        _seed_done_stop_report(repo, best_state_id="best-1")
        return repo

    def test_rejected_candidate_not_promoted(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="rejected"))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_expired_candidate_not_promoted(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="expired"))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_superseded_candidate_not_promoted(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="superseded"))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_deferred_candidate_not_promoted(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="deferred"))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_human_approved_candidate_not_promoted_again(self) -> None:
        repo = self._setup_repo()
        cand = self._make_candidate(state="approved")
        cand.decided_by = "human"
        cand.reviewer = "operator"
        repo.save_memory_candidate(cand)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_auto_approved_candidate_not_promoted_again(self) -> None:
        repo = self._setup_repo()
        cand = self._make_candidate(state="approved")
        cand.decided_by = "auto"
        cand.reviewer = "auto"
        repo.save_memory_candidate(cand)
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_expired_by_time_candidate_not_promoted(self) -> None:
        repo = self._setup_repo()
        expired_time = datetime.now(timezone.utc) - timedelta(days=1)
        repo.save_memory_candidate(self._make_candidate(expires_at=expired_time))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_pending_valid_candidate_promoted(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="proposed"))
        mgr = MemoryManager(repo)
        result = mgr.auto_promote("t1")
        assert len(result) == 1

    def test_expired_at_boundary_not_promoted(self) -> None:
        repo = self._setup_repo()
        # Candidate that expired a second ago (clearly past expiry)
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        repo.save_memory_candidate(self._make_candidate(expires_at=expired_time))
        mgr = MemoryManager(repo)
        assert mgr.auto_promote("t1") == []

    def test_retry_does_not_revive_final_decision(self) -> None:
        repo = self._setup_repo()
        repo.save_memory_candidate(self._make_candidate(state="rejected"))
        mgr = MemoryManager(repo)
        mgr.auto_promote("t1")
        mgr.auto_promote("t1")
        assert repo.list_promoted_memories("t1") == []
