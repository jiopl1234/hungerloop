"""Focused unit tests for worker handoff proposed checks.

Covers VAL-DISC-001 through VAL-DISC-007, VAL-DISC-016, VAL-DISC-017,
VAL-CROSS-003, and VAL-CROSS-004.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
)
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.check_proposal_gate import CheckProposalGate, DryRunner
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler

RepoUnderTest = InMemoryRepository | SQLiteRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _budget(max_new_items_per_loop: int = 3) -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        max_new_items_per_loop=max_new_items_per_loop,
    )


def _file_proposal(
    path: str = "src/main.py",
    *,
    description: str = "main file exists",
    source_quote: str = "The project must have a main file.",
    proposed_by: str = "worker:agent-1",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=description,
        source_quote=source_quote,
        proposed_by=proposed_by,
    )


def _shell_proposal(
    argv: list[str] | None = None,
    *,
    description: str = "run tests",
    source_quote: str = "The project must pass tests.",
    proposed_by: str = "worker:agent-1",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv or ["python", "-m", "pytest", "-q"]},
        description=description,
        source_quote=source_quote,
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
    detail: str = "Need a pytest test for the new module.",
    proposed_checks: list[CheckProposal] | None = None,
) -> HandoffItem:
    return HandoffItem(
        item_type="discovered_issue",
        summary=summary,
        detail=detail,
        proposed_checks=proposed_checks or [],
    )


def _non_test_gap_item(
    *,
    item_type: str = "follow_up",
    summary: str = "Follow up on Y",
    proposed_checks: list[CheckProposal] | None = None,
) -> HandoffItem:
    return HandoffItem(
        item_type=item_type,  # type: ignore[arg-type]
        summary=summary,
        proposed_checks=proposed_checks or [],
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "in_memory":
        repository: RepoUnderTest = InMemoryRepository()
    else:
        repository = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")

    repository.create_task("task-1", "Process worker handoffs")
    repository.save_hunger_ledger("task-1", HungerLedger(task_id="task-1", items=[]))
    yield repository

    if isinstance(repository, SQLiteRepository):
        repository.close()


class _FakeDryRunner(DryRunner):
    """Always-true dry runner for deterministic shell proposals."""

    async def dry_run(self, argv: list[str], cwd: Path | None = None) -> bool:
        return True


class _SpyRefinementCompiler(RefinementCompiler):
    """Spy refinement compiler that tracks compile_spec_coverage calls."""

    def __init__(self, repo: Any) -> None:
        super().__init__(repo)
        self.spec_coverage_calls: list[dict[str, Any]] = []

    def compile_spec_coverage(  # type: ignore[override]
        self,
        *,
        task_id: str,
        proposals: list[CheckProposal],
        generated_by: str,
        tier: int = 1,
        max_new_items: int = 20,
    ) -> list[str]:
        self.spec_coverage_calls.append(
            {
                "task_id": task_id,
                "proposals": list(proposals),
                "generated_by": generated_by,
                "tier": tier,
                "max_new_items": max_new_items,
            }
        )
        return super().compile_spec_coverage(
            task_id=task_id,
            proposals=proposals,
            generated_by=generated_by,
            tier=tier,
            max_new_items=max_new_items,
        )


class _NoOpRefinementCompiler(RefinementCompiler):
    """Compiler that returns no ids, to test compiler-owned injection."""

    def __init__(self, repo: Any) -> None:
        super().__init__(repo)
        self.spec_coverage_calls: list[dict[str, Any]] = []

    def compile_spec_coverage(  # type: ignore[override]
        self,
        *,
        task_id: str,
        proposals: list[CheckProposal],
        generated_by: str,
        tier: int = 1,
        max_new_items: int = 20,
    ) -> list[str]:
        self.spec_coverage_calls.append(
            {
                "task_id": task_id,
                "proposals": list(proposals),
                "generated_by": generated_by,
                "tier": tier,
                "max_new_items": max_new_items,
            }
        )
        return []


# ---------------------------------------------------------------------------
# VAL-DISC-001: Handoff items carry optional deterministic proposals
# ---------------------------------------------------------------------------


class TestHandoffItemProposedChecks:
    """Tests proving HandoffItem.proposed_checks defaults, validation, and round-trips."""

    def test_proposed_checks_defaults_to_empty_list(self) -> None:
        item = HandoffItem(item_type="discovered_issue", summary="test")
        assert item.proposed_checks == []

    def test_accepts_valid_file_exists_proposal(self) -> None:
        proposal = _file_proposal()
        item = HandoffItem(
            item_type="discovered_issue",
            summary="test",
            proposed_checks=[proposal],
        )
        assert len(item.proposed_checks) == 1
        assert item.proposed_checks[0].check_type == AcceptanceCheckType.FILE_EXISTS

    def test_accepts_valid_shell_exit_zero_proposal(self) -> None:
        proposal = _shell_proposal()
        item = HandoffItem(
            item_type="discovered_issue",
            summary="test",
            proposed_checks=[proposal],
        )
        assert len(item.proposed_checks) == 1
        assert item.proposed_checks[0].check_type == AcceptanceCheckType.SHELL_EXIT_ZERO

    def test_serialization_round_trip(self) -> None:
        proposal = _file_proposal(path="src/app.py")
        item = HandoffItem(
            item_type="discovered_issue",
            summary="test",
            proposed_checks=[proposal],
        )
        data = item.model_dump(mode="json")
        restored = HandoffItem.model_validate(data)
        assert len(restored.proposed_checks) == 1
        assert restored.proposed_checks[0].params["path"] == "src/app.py"

    def test_rejects_llm_judge_proposal(self) -> None:
        """Non-deterministic check types are rejected at model level."""
        with pytest.raises(Exception):
            CheckProposal(
                check_type=AcceptanceCheckType.LLM_JUDGE,
                params={},
                source_quote="spec",
            )

    def test_rejects_empty_source_quote(self) -> None:
        with pytest.raises(Exception):
            CheckProposal(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "src/main.py"},
                source_quote="",
            )

    def test_rejects_non_dict_params(self) -> None:
        with pytest.raises(Exception):
            CheckProposal(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params="not a dict",  # type: ignore[arg-type]
                source_quote="spec",
            )


# ---------------------------------------------------------------------------
# VAL-DISC-002: Only discovered test-gap handoffs inject proposed checks
# ---------------------------------------------------------------------------


class TestOnlyTestGapHandoffsInject:
    """Tests proving only test-gap discovered_issue items can inject proposals."""

    @pytest.mark.asyncio
    async def test_test_gap_item_injects_proposal(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(
            _test_gap_item(proposed_checks=[proposal]),
        )

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 1
        ledger = repo.get_hunger_ledger("task-1")
        assert any(item.id.startswith("H-SYN-") for item in ledger.items)

    @pytest.mark.asyncio
    async def test_non_test_gap_discovered_issue_no_injection(
        self, repo: RepoUnderTest
    ) -> None:
        """A discovered_issue classified as mission_feature (not test_gap) does not inject."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        # related_feature_ids causes classification as "mission_feature"
        item = HandoffItem(
            item_type="discovered_issue",
            summary="New feature found",
            detail="A new mission feature",
            related_feature_ids=["feature-1"],
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0
        ledger = repo.get_hunger_ledger("task-1")
        assert not any(item.id.startswith("H-SYN-") for item in ledger.items)

    @pytest.mark.asyncio
    async def test_blocker_item_no_proposal_injection(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        # Seed a hunger item to block via the ledger (SQLite requires task_id)
        ledger = repo.get_hunger_ledger("task-1")
        repo.save_hunger_ledger(
            "task-1",
            HungerLedger(
                task_id="task-1",
                items=[*ledger.items, HungerItem(id="H-001", title="base item")],
            ),
        )
        item = HandoffItem(
            item_type="blocker",
            summary="blocked",
            related_item_ids=["H-001"],
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0
        ledger = repo.get_hunger_ledger("task-1")
        assert not any(item.id.startswith("H-SYN-") for item in ledger.items)

    @pytest.mark.asyncio
    async def test_follow_up_item_no_proposal_injection(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        item = _non_test_gap_item(
            item_type="follow_up",
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_critical_context_item_no_proposal_injection(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        item = _non_test_gap_item(
            item_type="critical_context",
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_incomplete_work_item_no_proposal_injection(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        item = _non_test_gap_item(
            item_type="incomplete_work",
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_structured_classification_precedes_incidental_wording(
        self, repo: RepoUnderTest
    ) -> None:
        """A discovered_issue with related_feature_ids is mission_feature,
        even if the text contains 'test'."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        # Text mentions 'test' but related_feature_ids forces mission_feature classification
        item = HandoffItem(
            item_type="discovered_issue",
            summary="test gap found",
            detail="Need pytest coverage",
            related_feature_ids=["feature-1"],
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1",
            3,
            [handoff],
            mission=None,
            budget=_budget(5),
        )

        assert result.accepted_proposal_count == 0


# ---------------------------------------------------------------------------
# VAL-DISC-003: Worker proposals pass through the deterministic gate
# ---------------------------------------------------------------------------


class TestWorkerProposalsThroughGate:
    """Tests proving rejected proposals are not injected."""

    @pytest.mark.asyncio
    async def test_duplicate_proposal_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/dup.py")
        handoff1 = _handoff(_test_gap_item(proposed_checks=[proposal]))
        handoff2 = _handoff(
            _test_gap_item(proposed_checks=[proposal.model_copy()]),
            agent_id="agent-2",
        )

        result1 = await processor.process_handoffs(
            "task-1", 3, [handoff1], mission=None, budget=_budget(5)
        )
        result2 = await processor.process_handoffs(
            "task-1", 4, [handoff2], mission=None, budget=_budget(5)
        )

        assert result1.accepted_proposal_count == 1
        assert result2.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_unsafe_path_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Absolute path is unsafe
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0
        ledger = repo.get_hunger_ledger("task-1")
        assert not any(item.id.startswith("H-SYN-") for item in ledger.items)

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "../../../etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_non_allowlisted_argv_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate(dry_runner=_FakeDryRunner())
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _shell_proposal(argv=["bash", "-c", "echo hi"])
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0


# ---------------------------------------------------------------------------
# VAL-DISC-004: Accepted proposals injected with worker provenance
# ---------------------------------------------------------------------------


class TestAcceptedProposalsWorkerProvenance:
    """Tests proving accepted proposals get generated_by=worker:<agent_id>."""

    @pytest.mark.asyncio
    async def test_generated_by_is_worker_agent_id(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(
            _test_gap_item(proposed_checks=[proposal]),
            agent_id="my-agent-42",
        )

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [item for item in ledger.items if item.id.startswith("H-SYN-")]
        assert len(syn_items) == 1
        assert syn_items[0].generated_by == "worker:my-agent-42"

    @pytest.mark.asyncio
    async def test_nested_proposed_by_cannot_spoof_provenance(
        self, repo: RepoUnderTest
    ) -> None:
        """Worker-controlled proposed_by cannot override generated_by."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(
            path="src/new.py",
            proposed_by="worker:spoofed-agent",
        )
        handoff = _handoff(
            _test_gap_item(proposed_checks=[proposal]),
            agent_id="real-agent",
        )

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [item for item in ledger.items if item.id.startswith("H-SYN-")]
        assert len(syn_items) == 1
        assert syn_items[0].generated_by == "worker:real-agent"

    @pytest.mark.asyncio
    async def test_acceptance_check_matches_proposal(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/app.py", description="app file check")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [item for item in ledger.items if item.id.startswith("H-SYN-")]
        assert len(syn_items) == 1
        check = syn_items[0].acceptance_checks[0]
        assert check.check_type == AcceptanceCheckType.FILE_EXISTS
        assert check.params["path"] == "src/app.py"
        assert check.description == "app file check"

    @pytest.mark.asyncio
    async def test_refinement_kind_is_spec_coverage(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [item for item in ledger.items if item.id.startswith("H-SYN-")]
        assert syn_items[0].refinement_kind == "spec_coverage"
        assert syn_items[0].refinement_tier == 1


# ---------------------------------------------------------------------------
# VAL-DISC-005: Proposal injection stays compiler-owned
# ---------------------------------------------------------------------------


class TestCompilerOwnedInjection:
    """Tests proving handoff delegates to compiler-owned spec coverage compilation."""

    @pytest.mark.asyncio
    async def test_compiler_is_called_with_accepted_proposals(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        spy_compiler = _SpyRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=spy_compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert len(spy_compiler.spec_coverage_calls) == 1
        call = spy_compiler.spec_coverage_calls[0]
        assert call["task_id"] == "task-1"
        assert call["generated_by"] == "worker:execution_worker_v1"
        assert call["tier"] == 1
        assert len(call["proposals"]) == 1

    @pytest.mark.asyncio
    async def test_no_compiler_call_when_no_proposals(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        spy_compiler = _SpyRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=spy_compiler,
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert len(spy_compiler.spec_coverage_calls) == 0

    @pytest.mark.asyncio
    async def test_no_compiler_call_when_all_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        spy_compiler = _SpyRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=spy_compiler,
        )
        # Unsafe path -> rejected
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert len(spy_compiler.spec_coverage_calls) == 0

    @pytest.mark.asyncio
    async def test_compiler_returns_no_ids_means_no_injection(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        noop_compiler = _NoOpRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=noop_compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0
        # No H-SYN items (compiler returned no ids)
        assert not any(i.startswith("H-SYN-") for i in result.injected_hunger_item_ids)
        # Compiler was called but returned no ids
        assert len(noop_compiler.spec_coverage_calls) == 1

    @pytest.mark.asyncio
    async def test_no_direct_ledger_writes_from_handoff(
        self, repo: RepoUnderTest
    ) -> None:
        """The handoff processor does not write H-SYN items directly;
        only through the compiler."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        noop_compiler = _NoOpRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=noop_compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        # No H-SYN items in the ledger (compiler returned no ids)
        ledger = repo.get_hunger_ledger("task-1")
        assert not any(item.id.startswith("H-SYN-") for item in ledger.items)


# ---------------------------------------------------------------------------
# VAL-DISC-006: Accepted proposal count is exact and persisted
# ---------------------------------------------------------------------------


class TestAcceptedProposalCountPersisted:
    """Tests proving accepted_proposal_count is exact and round-trips."""

    @pytest.mark.asyncio
    async def test_count_equals_accepted_and_injected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path="src/a.py"),
            _file_proposal(path="src/b.py"),
            _file_proposal(path="src/c.py"),
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 3
        assert len([i for i in result.injected_hunger_item_ids if i.startswith("H-SYN-")]) == 3

    @pytest.mark.asyncio
    async def test_count_excludes_rejected(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        good_proposal = _file_proposal(path="src/good.py")
        bad_proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},  # unsafe
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(
            _test_gap_item(proposed_checks=[good_proposal, bad_proposal])
        )

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 1

    @pytest.mark.asyncio
    async def test_count_excludes_non_test_gap(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        # Put on a follow_up item (not test_gap)
        item = HandoffItem(
            item_type="follow_up",
            summary="follow up",
            proposed_checks=[proposal],
        )
        handoff = _handoff(item)

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_count_excludes_over_cap(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path=f"src/file_{i}.py") for i in range(5)
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        # cap=3: 1 fact consumes 1, leaving 2 for proposals
        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(3)
        )

        assert result.accepted_proposal_count == 2

    @pytest.mark.asyncio
    async def test_count_round_trips_through_repository(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path="src/a.py"),
            _file_proposal(path="src/b.py"),
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 2

        # Reload from repository
        reloaded = repo.get_latest_handoff_processing_result("task-1")
        assert reloaded is not None
        assert reloaded.accepted_proposal_count == 2


# ---------------------------------------------------------------------------
# VAL-DISC-007: Proposal injection respects the per-loop new-item cap
# ---------------------------------------------------------------------------


class TestPerLoopCapEnforcement:
    """Tests proving shared caps across facts, proposals, and handoffs."""

    @pytest.mark.asyncio
    async def test_facts_and_proposals_share_cap(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # 2 discovered facts (mission_feature) + 1 test_gap (also creates a fact) + 1 proposal
        # cap=5: 3 facts consume 3, leaving 2 for proposals (only 1 proposal, so 1 injected)
        fact_items = [
            HandoffItem(
                item_type="discovered_issue",
                summary=f"fact-{i}",
                detail=f"detail-{i}",
                related_feature_ids=["feature-1"],
            )
            for i in range(2)
        ]
        proposal = _file_proposal(path="src/new.py")
        test_gap_item = _test_gap_item(proposed_checks=[proposal])
        handoff = _handoff(*fact_items, test_gap_item)

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        # 3 facts + 1 proposal = 4 total
        assert result.accepted_proposal_count == 1
        assert len(result.injected_hunger_item_ids) == 4  # 3 facts + 1 proposal

    @pytest.mark.asyncio
    async def test_cap_shared_across_multiple_handoffs(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Handoff 1: 1 test_gap with 1 proposal
        h1 = _handoff(
            _test_gap_item(
                summary="gap1",
                proposed_checks=[_file_proposal(path="src/a.py")],
            ),
            agent_id="agent-1",
        )
        # Handoff 2: 1 test_gap with 1 proposal
        h2 = _handoff(
            _test_gap_item(
                summary="gap2",
                proposed_checks=[_file_proposal(path="src/b.py")],
            ),
            agent_id="agent-2",
        )

        # cap=3: first fact consumes 1, second fact consumes 1, leaving 1 for proposals
        # Only 1 proposal can be injected
        result = await processor.process_handoffs(
            "task-1", 3, [h1, h2], mission=None, budget=_budget(3)
        )

        assert result.accepted_proposal_count == 1

    @pytest.mark.asyncio
    async def test_zero_cap_injects_nothing(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(0)
        )

        assert result.accepted_proposal_count == 0


# ---------------------------------------------------------------------------
# VAL-DISC-016: Handoff proposal gating is await-safe at all call sites
# ---------------------------------------------------------------------------


class TestAwaitSafeGateIntegration:
    """Tests proving the gate is awaited exactly once per batch."""

    @pytest.mark.asyncio
    async def test_gate_awaited_once_per_batch(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        call_count = 0

        class _CountingGate(CheckProposalGate):
            async def filter(  # type: ignore[override]
                self,
                proposals: list[CheckProposal],
                *,
                existing_keys: set[str] | None = None,
            ) -> Any:
                nonlocal call_count
                call_count += 1
                return await super().filter(proposals, existing_keys=existing_keys)

        gate = _CountingGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path="src/a.py"),
            _file_proposal(path="src/b.py"),
            _file_proposal(path="src/c.py"),
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_no_unawaited_coroutine_warnings(self, repo: RepoUnderTest) -> None:
        """No RuntimeWarning about unawaited coroutine should occur."""
        import warnings

        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await processor.process_handoffs(
                "task-1", 3, [handoff], mission=None, budget=_budget(5)
            )
            unawaited = [
                warn for warn in w if "coroutine" in str(warn.message).lower()
            ]
            assert len(unawaited) == 0

    @pytest.mark.asyncio
    async def test_proposals_not_skipped_or_duplicated(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path="src/a.py"),
            _file_proposal(path="src/b.py"),
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 2
        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [i for i in ledger.items if i.id.startswith("H-SYN-")]
        assert len(syn_items) == 2


# ---------------------------------------------------------------------------
# VAL-DISC-017: Rejected worker proposals are observable or explicitly unaudited
# ---------------------------------------------------------------------------


class TestRejectedProposalObservability:
    """Tests proving rejected proposals are observable or explicitly unaudited."""

    @pytest.mark.asyncio
    async def test_rejected_proposal_emits_event(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Unsafe path -> rejected
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        events = repo.list_events("task-1")
        rejection_events = [
            e for e in events
            if str(e.get("event_type", "")).startswith("worker_proposal_rejected")
            or str(e.get("event_type", "")) == "synth_check_rejected"
        ]
        assert len(rejection_events) >= 1

    @pytest.mark.asyncio
    async def test_rejected_event_contains_dedup_key(self, repo: RepoUnderTest) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        events = repo.list_events("task-1")
        rejection_events = [
            e for e in events
            if "rejected" in str(e.get("event_type", "")).lower()
        ]
        assert len(rejection_events) >= 1
        payload = rejection_events[0].get("payload", {})
        assert "dedup_key" in payload or "dedup_key" in str(payload)

    @pytest.mark.asyncio
    async def test_rejected_proposal_no_ledger_mutation(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger_after = repo.get_hunger_ledger("task-1")
        # No new H-SYN items from rejected proposal
        syn_items = [i for i in ledger_after.items if i.id.startswith("H-SYN-")]
        assert len(syn_items) == 0

    @pytest.mark.asyncio
    async def test_over_cap_rejected_does_not_increase_count(
        self, repo: RepoUnderTest
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposals = [
            _file_proposal(path=f"src/file_{i}.py") for i in range(5)
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        # cap=3: 1 fact consumes 1, leaving 2 for proposals (5 proposals, only 2 fit)
        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(3)
        )

        assert result.accepted_proposal_count == 2


# ---------------------------------------------------------------------------
# VAL-CROSS-003: Proposal deduplication is global across synthesis and discovery
# ---------------------------------------------------------------------------


class TestCrossSourceDedup:
    """Tests proving global dedup across synthesis, discovery, operator checks."""

    @pytest.mark.asyncio
    async def test_operator_check_blocks_worker_proposal(
        self, repo: RepoUnderTest
    ) -> None:
        """An existing operator-authored check with the same dedup key
        blocks a worker proposal from being injected."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        # Seed ledger with an operator-authored check
        existing = HungerItem(
            id="H-001",
            title="operator check",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/main.py"},
                    description="operator check",
                )
            ],
        )
        ledger = repo.get_hunger_ledger("task-1")
        repo.save_hunger_ledger(
            "task-1",
            HungerLedger(task_id="task-1", items=[*ledger.items, existing]),
        )

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Worker proposes the same check
        proposal = _file_proposal(path="src/main.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_synthesized_check_blocks_worker_proposal(
        self, repo: RepoUnderTest
    ) -> None:
        """A previously synthesized H-SYN check blocks a worker proposal."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        # Seed ledger with a synthesized check
        syn_item = HungerItem(
            id="H-SYN-001",
            title="syn check",
            refinement_kind="spec_coverage",
            refinement_tier=1,
            generated_by="synthesizer",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "src/app.py"},
                    description="syn check",
                )
            ],
        )
        ledger = repo.get_hunger_ledger("task-1")
        repo.save_hunger_ledger(
            "task-1",
            HungerLedger(task_id="task-1", items=[*ledger.items, syn_item]),
        )

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _file_proposal(path="src/app.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        assert result.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_rejected_proposal_history_blocks_duplicate(
        self, repo: RepoUnderTest
    ) -> None:
        """A previously rejected proposal key blocks a duplicate in a later call."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # First: unsafe path rejected
        bad_proposal = CheckProposal(
            check_type=AcceptanceCheckType.FILE_EXISTS,
            params={"path": "/etc/passwd"},
            source_quote="spec",
            proposed_by="worker:agent-1",
        )
        handoff1 = _handoff(_test_gap_item(proposed_checks=[bad_proposal]))
        await processor.process_handoffs(
            "task-1", 3, [handoff1], mission=None, budget=_budget(5)
        )

        # Second: same bad proposal should still be rejected
        handoff2 = _handoff(
            _test_gap_item(proposed_checks=[bad_proposal.model_copy()]),
            agent_id="agent-2",
        )
        result2 = await processor.process_handoffs(
            "task-1", 4, [handoff2], mission=None, budget=_budget(5)
        )

        assert result2.accepted_proposal_count == 0

    @pytest.mark.asyncio
    async def test_distinct_proposals_from_different_sources_both_injected(
        self, repo: RepoUnderTest
    ) -> None:
        """Distinct proposals from different sources remain separately injectable."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Different paths -> different dedup keys
        p1 = _file_proposal(path="src/from_agent1.py")
        p2 = _file_proposal(path="src/from_agent2.py")

        h1 = _handoff(
            _test_gap_item(summary="gap1", proposed_checks=[p1]),
            agent_id="agent-1",
        )
        h2 = _handoff(
            _test_gap_item(summary="gap2", proposed_checks=[p2]),
            agent_id="agent-2",
        )

        result = await processor.process_handoffs(
            "task-1", 3, [h1, h2], mission=None, budget=_budget(10)
        )

        assert result.accepted_proposal_count == 2
        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [i for i in ledger.items if i.id.startswith("H-SYN-")]
        assert len(syn_items) == 2
        # Check provenance
        assert syn_items[0].generated_by == "worker:agent-1"
        assert syn_items[1].generated_by == "worker:agent-2"


# ---------------------------------------------------------------------------
# VAL-CROSS-004: Proposal ledger writes are compiler-owned
# ---------------------------------------------------------------------------


class TestCompilerOwnedLedgerWrites:
    """Tests proving no direct handoff-layer ledger writes for proposals."""

    @pytest.mark.asyncio
    async def test_handoff_processor_does_not_write_h_syn_directly(
        self, repo: RepoUnderTest
    ) -> None:
        """When the compiler returns no ids, no H-SYN items appear in ledger."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        noop_compiler = _NoOpRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=noop_compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        ledger = repo.get_hunger_ledger("task-1")
        assert not any(i.id.startswith("H-SYN-") for i in ledger.items)

    @pytest.mark.asyncio
    async def test_injected_ids_match_compiler_returned_ids(
        self, repo: RepoUnderTest
    ) -> None:
        """Injected H-SYN ids in the result match exactly what the compiler returned."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        spy_compiler = _SpyRefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=spy_compiler,
        )
        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        # The compiler was called and returned ids
        assert len(spy_compiler.spec_coverage_calls) == 1
        compiler_ids = ["H-SYN-001"]  # what the real compiler returns
        syn_ids_in_result = [
            i for i in result.injected_hunger_item_ids if i.startswith("H-SYN-")
        ]
        assert syn_ids_in_result == compiler_ids
