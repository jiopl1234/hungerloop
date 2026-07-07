"""Focused unit tests for DISCOVERY_CREDIT events and reports.

Covers VAL-DISC-008 through VAL-DISC-015.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    HungerItemType,
    LoopPhase,
    StopReason,
    ValidationVerdict,
)
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
)
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate, DryRunner
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler
from hungerloop.services.stop_report_builder import build_stop_report
from hungerloop.services.workspace_manager import WorkspaceManager

RepoUnderTest = InMemoryRepository | SQLiteRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _budget(max_new_items_per_loop: int = 5) -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        max_new_items_per_loop=max_new_items_per_loop,
    )


def _file_proposal(
    path: str = "src/main.py",
    *,
    proposed_by: str = "worker:agent-1",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=f"file exists: {path}",
        source_quote="The project must have files.",
        proposed_by=proposed_by,
    )


def _handoff(
    *items: HandoffItem,
    agent_id: str = "execution_worker_v1",
    task_id: str = "task-1",
    loop_id: int = 3,
) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id=agent_id,
        task_id=task_id,
        loop_id=loop_id,
        summary="Worker discovered test gaps with proposed checks.",
        handoff_items=list(items),
    )


def _test_gap_item(
    *,
    summary: str = "Missing test for X",
    proposed_checks: list[CheckProposal] | None = None,
) -> HandoffItem:
    return HandoffItem(
        item_type="discovered_issue",
        summary=summary,
        detail="Need a pytest test for the new module.",
        proposed_checks=proposed_checks or [],
    )


def _worker_h_syn_item(
    item_id: str = "H-SYN-001",
    *,
    generated_by: str = "worker:agent-1",
    check_type: AcceptanceCheckType = AcceptanceCheckType.FILE_EXISTS,
    path: str = "src/main.py",
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Worker check {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=check_type,
                params={"path": path},
                description=f"check {item_id}",
            )
        ],
        acceptance_mode="all",
        refinement_tier=1,
        refinement_kind="spec_coverage",
        generated_by=generated_by,
    )


def _candidate(task_id: str = "task-1", loop_id: int = 1) -> CandidateState:
    return CandidateState(
        id=f"CAND-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        summary="test candidate",
        workspace_ref=f"candidates/loop_{loop_id:03d}",
    )


def _report(
    *,
    task_id: str = "task-1",
    loop_id: int = 1,
    verdict: ValidationVerdict = ValidationVerdict.PASS,
    newly_passed: list[str] | None = None,
    regressed: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    currently_passed: list[str] | None = None,
) -> ValidationReport:
    return ValidationReport(
        id=f"VAL-{task_id}-{loop_id}",
        task_id=task_id,
        loop_id=loop_id,
        candidate_state_id=f"CAND-{task_id}-{loop_id}",
        baseline_state_id=None,
        verdict=verdict,
        newly_passed_check_keys=newly_passed or [],
        regressed_check_keys=regressed or [],
        missing_evidence=missing_evidence or [],
        currently_passed_check_keys=currently_passed or [],
        evidence_ids=["ev-1"],
        has_real_progress=bool(newly_passed),
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "in_memory":
        repository: RepoUnderTest = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Discovery credit test")
    repository.save_hunger_ledger("task-1", HungerLedger(task_id="task-1", items=[]))
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


class _FakeDryRunner(DryRunner):
    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        return True


# ---------------------------------------------------------------------------
# VAL-DISC-008: Discovery credit emitted when worker-generated check newly passes
# ---------------------------------------------------------------------------


class TestDiscoveryCreditEmission:
    """VAL-DISC-008: worker-generated check newly passes emits DISCOVERY_CREDIT."""

    def test_emits_credit_for_worker_generated_check(
        self, tmp_path: Path
    ) -> None:
        """A newly passed check key owned by a worker-generated H-SYN item
        emits exactly one DISCOVERY_CREDIT event on commit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        # Seed ledger with a worker-generated H-SYN item
        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        report = _report(
            newly_passed=["H-SYN-001:0"],
            currently_passed=["H-SYN-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 1
        payload = events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["proposer"] == "agent-1"
        assert payload["check_key"] == "H-SYN-001:0"
        assert payload["loop_id"] == 1

    def test_no_duplicate_credit_for_repeated_key_in_same_report(
        self, tmp_path: Path
    ) -> None:
        """Duplicate keys in one validation report emit only one credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        # Even if the same key appears twice, only one credit
        report = _report(
            newly_passed=["H-SYN-001:0", "H-SYN-001:0"],
            currently_passed=["H-SYN-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 1

    def test_no_credit_on_replay_commit(
        self, tmp_path: Path
    ) -> None:
        """Replayed commits (same key already credited) do not emit again."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        # Mark as already accepted
        worker_item.status = HungerItemStatus.VALIDATED_SATISFIED
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )
        # Save accepted check so it's already in the accepted set
        repo.save_accepted_check(
            task_id="task-1",
            check_key="H-SYN-001:0",
            hunger_item_id="H-SYN-001",
            check_index=0,
            accepted_at_loop=1,
            validation_id="VAL-old",
            evidence_id=None,
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 2)

        candidate = _candidate(loop_id=2)
        # The key is in newly_passed again (replay)
        report = _report(
            loop_id=2,
            newly_passed=["H-SYN-001:0"],
            currently_passed=["H-SYN-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        # No credit should be emitted because the key was already accepted
        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0


# ---------------------------------------------------------------------------
# VAL-DISC-009: No credit for non-worker or non-new progress
# ---------------------------------------------------------------------------


class TestNoCreditForNonWorkerOrNonNew:
    """VAL-DISC-009: no DISCOVERY_CREDIT for non-worker or non-new progress."""

    def test_no_credit_for_synthesizer_generated_item(
        self, tmp_path: Path
    ) -> None:
        """Synthesizer-generated H-SYN items emit no credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        syn_item = _worker_h_syn_item(
            "H-SYN-001", generated_by="synthesizer"
        )
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[syn_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        report = _report(
            newly_passed=["H-SYN-001:0"],
            currently_passed=["H-SYN-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0

    def test_no_credit_for_operator_item(
        self, tmp_path: Path
    ) -> None:
        """Operator-authored items (generated_by=None) emit no credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        op_item = HungerItem(
            id="H-001",
            title="operator check",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/app.py"},
                    description="operator check",
                )
            ],
        )
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[op_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        report = _report(
            newly_passed=["H-001:0"],
            currently_passed=["H-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0

    def test_no_credit_on_rejected_candidate(
        self, tmp_path: Path
    ) -> None:
        """Rejected candidates emit no credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        # Report with no new progress -> rejected
        report = _report(
            verdict=ValidationVerdict.FAIL,
            newly_passed=[],
            currently_passed=[],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is False

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0

    def test_no_credit_for_check_not_in_newly_passed(
        self, tmp_path: Path
    ) -> None:
        """A currently-passed but not newly-passed worker check emits no credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        # A different key is newly passed, H-SYN-001:0 is only currently passed
        report = _report(
            newly_passed=["H-002:0"],
            currently_passed=["H-SYN-001:0", "H-002:0"],
        )

        # Seed H-002 as well so commit succeeds
        other_item = HungerItem(
            id="H-002",
            title="other",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/other.py"},
                )
            ],
        )
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item, other_item])
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is True

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0

    def test_no_credit_on_missing_evidence(
        self, tmp_path: Path
    ) -> None:
        """Missing evidence rejects the candidate, no credit."""
        repo = InMemoryRepository()
        repo.create_task("task-1", "test")
        ws = WorkspaceManager(root=tmp_path / "ws")

        worker_item = _worker_h_syn_item("H-SYN-001", generated_by="worker:agent-1")
        repo.save_hunger_ledger(
            "task-1", HungerLedger(task_id="task-1", items=[worker_item])
        )

        cm = CommitManager(repo=repo, workspace_manager=ws)
        ws.create_candidate_workspace("task-1", 1)

        candidate = _candidate()
        report = _report(
            newly_passed=["H-SYN-001:0"],
            currently_passed=["H-SYN-001:0"],
            missing_evidence=["H-SYN-001:0"],
        )

        result = cm.apply(candidate, report)
        assert result["committed"] is False

        events = repo.list_events("task-1", event_types=[EventType.DISCOVERY_CREDIT.value])
        assert len(events) == 0


# ---------------------------------------------------------------------------
# VAL-DISC-010: Discovery credit events queryable in both repositories
# ---------------------------------------------------------------------------


class TestDiscoveryCreditQueryable:
    """VAL-DISC-010: events queryable in append order from both repos."""

    def test_events_queryable_in_append_order(self, repo: RepoUnderTest) -> None:
        """DISCOVERY_CREDIT events are returned in append order with
        decoded payloads, and unrelated events are excluded."""
        # Emit several discovery credit events
        for i in range(3):
            repo.append_event(
                EventType.DISCOVERY_CREDIT,
                {
                    "proposer": f"agent-{i}",
                    "check_key": f"H-SYN-00{i+1}:0",
                    "loop_id": i + 1,
                },
                task_id="task-1",
                loop_id=i + 1,
            )
        # Emit an unrelated event
        repo.append_event(
            EventType.CANDIDATE_COMMITTED,
            {"candidate_state_id": "CAND-1"},
            task_id="task-1",
            loop_id=1,
        )

        events = repo.list_events(
            "task-1", event_types=[EventType.DISCOVERY_CREDIT.value]
        )
        assert len(events) == 3
        # Verify append order
        for i, event in enumerate(events):
            assert event["event_type"] == EventType.DISCOVERY_CREDIT.value
            payload = event["payload"]
            assert isinstance(payload, dict)
            assert payload["proposer"] == f"agent-{i}"
            assert payload["check_key"] == f"H-SYN-00{i+1}:0"
            assert payload["loop_id"] == i + 1


# ---------------------------------------------------------------------------
# VAL-DISC-011: Accepted proposals reset no-progress streak once per loop
# ---------------------------------------------------------------------------


class TestStreakResetOnAcceptedProposals:
    """VAL-DISC-011: accepted proposals reset streak exactly once per loop."""

    def test_accepted_proposals_reset_streak_once(
        self, repo: RepoUnderTest
    ) -> None:
        """Multiple accepted proposals reset the streak exactly once."""
        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # Set a non-zero streak
        repo.increment_no_progress_streak("task-1")
        repo.increment_no_progress_streak("task-1")

        # Create handoff with multiple proposals
        p1 = _file_proposal(path="src/a.py")
        p2 = _file_proposal(path="src/b.py")
        p3 = _file_proposal(path="src/c.py")
        handoff = _handoff(
            _test_gap_item(proposed_checks=[p1, p2, p3])
        )

        import asyncio
        result = asyncio.run(
            processor.process_handoffs(
                "task-1", 3, [handoff], mission=None, budget=_budget(10)
            )
        )

        assert result.accepted_proposal_count == 3

        # The orchestrator should reset the streak once when
        # accepted_proposal_count > 0.  We simulate that here.
        if result.accepted_proposal_count > 0:
            repo.reset_no_progress_streak("task-1")

        # Verify streak is 0 by incrementing and checking the return value.
        # After reset, increment should return 1.
        next_streak = repo.increment_no_progress_streak("task-1")
        assert next_streak == 1


# ---------------------------------------------------------------------------
# VAL-DISC-012: Rejected or absent proposals do not reset streak
# ---------------------------------------------------------------------------


class TestNoStreakResetForRejectedOrAbsent:
    """VAL-DISC-012: rejected/absent proposals do not reset streak."""

    def test_no_reset_when_no_proposals(self, repo: RepoUnderTest) -> None:
        """No proposed checks -> no streak reset from proposal processing."""
        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # Set a non-zero streak
        repo.increment_no_progress_streak("task-1")
        repo.increment_no_progress_streak("task-1")

        # Handoff with no proposed checks
        handoff = _handoff(
            _test_gap_item(proposed_checks=[])
        )

        import asyncio
        result = asyncio.run(
            processor.process_handoffs(
                "task-1", 3, [handoff], mission=None, budget=_budget(10)
            )
        )

        assert result.accepted_proposal_count == 0
        # Streak should NOT be reset. Incrementing should return 3.
        next_streak = repo.increment_no_progress_streak("task-1")
        assert next_streak == 3

    def test_no_reset_when_all_proposals_rejected(self, repo: RepoUnderTest) -> None:
        """All proposals rejected -> no streak reset from proposal processing."""
        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # Set a non-zero streak
        repo.increment_no_progress_streak("task-1")

        # Unsafe path proposal -> will be rejected
        bad_proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            description="bad",
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(
            _test_gap_item(proposed_checks=[bad_proposal])
        )

        import asyncio
        result = asyncio.run(
            processor.process_handoffs(
                "task-1", 3, [handoff], mission=None, budget=_budget(10)
            )
        )

        assert result.accepted_proposal_count == 0
        # Streak should NOT be reset. Incrementing should return 2.
        next_streak = repo.increment_no_progress_streak("task-1")
        assert next_streak == 2


# ---------------------------------------------------------------------------
# VAL-DISC-013: Stop reports summarize discovery credits by proposer
# ---------------------------------------------------------------------------


class TestStopReportDiscoveryCredits:
    """VAL-DISC-013: stop reports persist proposer-to-count discovery credit summaries."""

    def test_stop_report_aggregates_credits_by_proposer(
        self, repo: RepoUnderTest
    ) -> None:
        """Building a stop report aggregates DISCOVERY_CREDIT events into
        a proposer-to-count mapping."""
        # Emit multiple credits from different proposers
        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-1", "check_key": "H-SYN-001:0", "loop_id": 1},
            task_id="task-1",
            loop_id=1,
        )
        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-1", "check_key": "H-SYN-002:0", "loop_id": 2},
            task_id="task-1",
            loop_id=2,
        )
        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-2", "check_key": "H-SYN-003:0", "loop_id": 2},
            task_id="task-1",
            loop_id=2,
        )

        report = build_stop_report(repo, "task-1", StopReason.DONE)
        assert report.discovery_credits == {"agent-1": 2, "agent-2": 1}

    def test_stop_report_empty_credits_when_none_exist(
        self, repo: RepoUnderTest
    ) -> None:
        """No DISCOVERY_CREDIT events -> empty discovery_credits mapping."""
        report = build_stop_report(repo, "task-1", StopReason.DONE)
        assert report.discovery_credits == {}

    def test_stop_report_round_trips_through_persistence(
        self, repo: RepoUnderTest
    ) -> None:
        """Discovery credits mapping is preserved when stop report is saved
        and read back."""
        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-1", "check_key": "H-SYN-001:0", "loop_id": 1},
            task_id="task-1",
            loop_id=1,
        )

        report = build_stop_report(repo, "task-1", StopReason.DONE)
        assert report.discovery_credits == {"agent-1": 1}

        repo.save_stop_report(report)
        loaded = repo.get_last_stop_report("task-1")
        assert loaded is not None
        assert loaded.discovery_credits == {"agent-1": 1}


# ---------------------------------------------------------------------------
# VAL-DISC-014: Discovery-credit summaries visible in user-facing reports
# ---------------------------------------------------------------------------


class TestCLIReportDiscoveryCredits:
    """VAL-DISC-014: CLI reports expose the persisted discovery credit summary."""

    def test_json_report_includes_discovery_credits(
        self, repo: RepoUnderTest
    ) -> None:
        """JSON report includes discovery_credits when events exist."""
        from hungerloop.cli.report_format import build_report_dict

        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-1", "check_key": "H-SYN-001:0", "loop_id": 1},
            task_id="task-1",
            loop_id=1,
        )
        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-2", "check_key": "H-SYN-002:0", "loop_id": 2},
            task_id="task-1",
            loop_id=2,
        )

        # Build and save stop report so it's persisted
        report = build_stop_report(repo, "task-1", StopReason.DONE)
        repo.save_stop_report(report)

        report_dict = build_report_dict(repo, "task-1")
        assert "discovery_credits" in report_dict
        assert report_dict["discovery_credits"] == {"agent-1": 1, "agent-2": 1}

    def test_json_report_empty_credits_when_none(
        self, repo: RepoUnderTest
    ) -> None:
        """JSON report omits or shows empty discovery_credits when no events."""
        from hungerloop.cli.report_format import build_report_dict

        report_dict = build_report_dict(repo, "task-1")
        # Should either omit or be empty dict
        credits = report_dict.get("discovery_credits", {})
        assert credits == {}

    def test_markdown_report_includes_discovery_credits(
        self, repo: RepoUnderTest
    ) -> None:
        """Markdown report includes discovery credits section when present."""
        from hungerloop.cli.report_format import build_report_dict, format_markdown

        repo.append_event(
            EventType.DISCOVERY_CREDIT,
            {"proposer": "agent-1", "check_key": "H-SYN-001:0", "loop_id": 1},
            task_id="task-1",
            loop_id=1,
        )

        report = build_stop_report(repo, "task-1", StopReason.DONE)
        repo.save_stop_report(report)

        report_dict = build_report_dict(repo, "task-1")
        md = format_markdown(report_dict)
        assert "Discovery credits" in md
        assert "agent-1" in md

    def test_markdown_report_omits_when_no_credits(
        self, repo: RepoUnderTest
    ) -> None:
        """Markdown report omits discovery credits section when none exist."""
        from hungerloop.cli.report_format import build_report_dict, format_markdown

        report_dict = build_report_dict(repo, "task-1")
        md = format_markdown(report_dict)
        assert "Discovery credits" not in md


# ---------------------------------------------------------------------------
# VAL-DISC-015: Discovery-credit APIs remain strict-type clean
# (Validated by mypy --strict in the verification step)
# ---------------------------------------------------------------------------
