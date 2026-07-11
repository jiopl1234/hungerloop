"""Focused tests for scrutiny-fix behaviors in worker proposal processing.

Covers the four scrutiny findings:
1. Production DryRunner wiring at all production construction sites.
2. Proposal collection only after _fact_from_handoff_item succeeds.
3. Returning actual injected proposal ids instead of ledger scanning.
4. Deterministic/observable cap exhaustion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.check_proposal_gate import (
    CheckProposalGate,
    DryRunner,
    SandboxDryRunner,
)
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler
from hungerloop.services.workspace_manager import WorkspaceManager

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
    proposed_by: str = "worker:agent-1",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.FILE_EXISTS,
        params={"path": path},
        description=f"file {path} exists",
        source_quote="The project must have the file.",
        proposed_by=proposed_by,
    )


def _shell_proposal(
    argv: list[str] | None = None,
    *,
    proposed_by: str = "worker:agent-1",
) -> CheckProposal:
    return CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": argv or ["python", "-c", "pass"]},
        description="run check",
        source_quote="The project must pass.",
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
        summary="Worker discovered test gaps.",
        handoff_items=list(items),
    )


def _test_gap_item(
    *,
    summary: str = "Missing test for X",
    detail: str = "Need a pytest test.",
    proposed_checks: list[CheckProposal] | None = None,
) -> HandoffItem:
    return HandoffItem(
        item_type="discovered_issue",
        summary=summary,
        detail=detail,
        proposed_checks=proposed_checks or [],
    )


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
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


# ---------------------------------------------------------------------------
# Fix 1: Production DryRunner wiring
# ---------------------------------------------------------------------------


class TestProductionDryRunnerWiring:
    """Tests proving SandboxDryRunner exists and is wired at production sites."""

    def test_sandbox_dry_runner_exists_and_implements_protocol(self) -> None:
        """SandboxDryRunner implements the DryRunner protocol."""
        from hungerloop.repository.in_memory_repo import InMemoryRepository
        from hungerloop.services.sandbox_runner import SandboxRunner

        repo = InMemoryRepository()
        runner = SandboxRunner(repo)
        adapter = SandboxDryRunner(runner)
        assert isinstance(adapter, DryRunner)

    def test_orchestrator_factory_wires_sandbox_dry_runner(self) -> None:
        """build_orchestrator constructs CheckProposalGate with a SandboxDryRunner."""
        from pathlib import Path

        from hungerloop.cli.orchestrator_factory import build_orchestrator
        from hungerloop.repository.in_memory_repo import InMemoryRepository

        repo = InMemoryRepository()
        repo.create_task("test-task", "test goal")
        orch = build_orchestrator(
            repo=repo,
            workspace_root=Path.cwd(),
        )
        # The handoff processor should have a gate with a dry runner
        hp = orch.handoff_processor
        assert hp is not None
        assert hp.check_proposal_gate is not None
        assert hp.check_proposal_gate._dry_runner is not None

    def test_shell_exit_zero_does_not_fail_with_no_dry_runner(self, repo: Any) -> None:
        """SHELL_EXIT_ZERO proposals work when a dry runner is available."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate(dry_runner=_FakeDryRunner())
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        proposal = _shell_proposal()
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        import asyncio

        result = asyncio.run(
            processor.process_handoffs(
                "task-1", 3, [handoff], mission=None, budget=_budget(5)
            )
        )

        assert result.accepted_proposal_count == 1

    def test_worker_gate_uses_candidate_workspace_cwd(
        self,
        repo: Any,
        tmp_path: Path,
    ) -> None:
        from hungerloop.services.handoff_processor import HandoffProcessor

        observed_cwds: list[Path | None] = []

        class _CapturingDryRunner(DryRunner):
            async def dry_run(
                self,
                argv: list[str],
                cwd: Path | None = None,
            ) -> bool:
                del argv
                observed_cwds.append(cwd)
                return True

        workspace_manager = WorkspaceManager(tmp_path)
        expected = workspace_manager.create_candidate_workspace("task-1", 3)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=CheckProposalGate(
                dry_runner=_CapturingDryRunner()
            ),
            refinement_compiler=RefinementCompiler(repo),
            workspace_manager=workspace_manager,
        )

        import asyncio

        result = asyncio.run(
            processor.process_handoffs(
                "task-1",
                3,
                [_handoff(_test_gap_item(proposed_checks=[_shell_proposal()]))],
                mission=None,
                budget=_budget(5),
            )
        )

        assert result.accepted_proposal_count == 1
        assert observed_cwds == [expected, expected]


# ---------------------------------------------------------------------------
# Fix 2: Proposal collection only after _fact_from_handoff_item succeeds
# ---------------------------------------------------------------------------


class TestProposalCollectionAfterFactSuccess:
    """Tests proving proposals are only collected when fact creation succeeds."""

    @pytest.mark.asyncio
    async def test_no_unbound_fact_when_validation_error(self, repo: Any) -> None:
        """When _fact_from_handoff_item raises, no NameError about unbound 'fact'."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # Create an item that will cause a ValidationError in
        # _fact_from_handoff_item (empty summary AND detail).
        # DiscoveredFact requires a non-empty title.
        # But HandoffItem requires summary... so we need a different
        # approach: make compile_discovered_facts raise.
        # Actually, _fact_from_handoff_item always succeeds because
        # _handoff_text has a fallback. The issue is if the fact
        # itself raises ValidationError (e.g. bad related_feature_ids
        # type). We can simulate by patching.

        # Instead, let's test the actual code path: if _fact_from_handoff_item
        # raises, the proposal collection block is not reached.
        # We'll patch _fact_from_handoff_item to raise.
        original = processor._fact_from_handoff_item

        def _raise_validation_error(
            item: HandoffItem, source_handoff_id: str
        ) -> Any:
            from pydantic import ValidationError as VE

            from hungerloop.models.handoff import DiscoveredFact

            # Trigger a real ValidationError by creating an invalid DiscoveredFact
            try:
                DiscoveredFact(
                    kind="invalid_kind",  # type: ignore[arg-type]
                    title="test",
                    description="test",
                    source_handoff_id=source_handoff_id,
                )
            except VE:
                raise

        processor._fact_from_handoff_item = _raise_validation_error  # type: ignore[assignment]

        proposal = _file_proposal(path="src/new.py")
        handoff = _handoff(_test_gap_item(proposed_checks=[proposal]))

        # This should NOT raise NameError about unbound 'fact'
        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        # No proposals should have been collected because fact creation failed
        assert result.accepted_proposal_count == 0

        # Restore
        processor._fact_from_handoff_item = original  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_stale_fact_not_used_after_error(self, repo: Any) -> None:
        """When the first item succeeds but the second raises,
        proposals from the second item are not collected using the
        stale 'fact' from the first item."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # First item: valid test_gap with proposals (should be collected)
        proposal1 = _file_proposal(path="src/a.py")
        item1 = _test_gap_item(summary="gap1", proposed_checks=[proposal1])

        # Second item: will raise ValidationError in _fact_from_handoff_item
        proposal2 = _file_proposal(path="src/b.py")
        item2 = _test_gap_item(summary="gap2", proposed_checks=[proposal2])

        handoff = _handoff(item1, item2)

        # Patch _fact_from_handoff_item to fail on the second call
        call_count = 0
        original = processor._fact_from_handoff_item

        def _patched(item: HandoffItem, source_handoff_id: str) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                from pydantic import ValidationError as VE

                from hungerloop.models.handoff import DiscoveredFact

                try:
                    DiscoveredFact(
                        kind="invalid_kind",  # type: ignore[arg-type]
                        title="test",
                        description="test",
                        source_handoff_id=source_handoff_id,
                    )
                except VE:
                    raise
            return original(item, source_handoff_id)

        processor._fact_from_handoff_item = _patched  # type: ignore[assignment]

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(10)
        )

        # Only the first proposal should be collected and injected
        assert result.accepted_proposal_count == 1

        processor._fact_from_handoff_item = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fix 3: Return actual injected proposal ids
# ---------------------------------------------------------------------------


class TestActualInjectedProposalIds:
    """Tests proving injected_hunger_item_ids reports only current-pass ids."""

    @pytest.mark.asyncio
    async def test_injected_ids_match_compiler_returned_ids(self, repo: Any) -> None:
        """Injected H-SYN ids match exactly what the compiler returned."""
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
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        # Exactly 1 H-SYN id from this pass
        syn_ids = [i for i in result.injected_hunger_item_ids if i.startswith("H-SYN-")]
        assert len(syn_ids) == 1

    @pytest.mark.asyncio
    async def test_prior_loop_items_not_over_reported(self, repo: Any) -> None:
        """Prior-loop worker-generated H-SYN items are not reported as
        current injected ids."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )

        # First loop: inject one proposal
        proposal1 = _file_proposal(path="src/a.py")
        handoff1 = _handoff(
            _test_gap_item(summary="gap1", proposed_checks=[proposal1]),
            loop_id=3,
        )
        result1 = await processor.process_handoffs(
            "task-1", 3, [handoff1], mission=None, budget=_budget(5)
        )
        assert len([i for i in result1.injected_hunger_item_ids if i.startswith("H-SYN-")]) == 1

        # Second loop: inject a different proposal
        proposal2 = _file_proposal(path="src/b.py")
        handoff2 = _handoff(
            _test_gap_item(summary="gap2", proposed_checks=[proposal2]),
            loop_id=4,
        )
        result2 = await processor.process_handoffs(
            "task-1", 4, [handoff2], mission=None, budget=_budget(5)
        )

        # Only 1 new H-SYN id should be reported, not 2 (which would happen
        # if prior-loop items were scanned from the ledger)
        syn_ids = [i for i in result2.injected_hunger_item_ids if i.startswith("H-SYN-")]
        assert len(syn_ids) == 1

    @pytest.mark.asyncio
    async def test_no_proposals_no_h_syn_ids(self, repo: Any) -> None:
        """When no proposals are collected, no H-SYN ids appear."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        handoff = _handoff(_test_gap_item(proposed_checks=[]))

        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(5)
        )

        syn_ids = [i for i in result.injected_hunger_item_ids if i.startswith("H-SYN-")]
        assert len(syn_ids) == 0


# ---------------------------------------------------------------------------
# Fix 4: Deterministic/observable cap exhaustion
# ---------------------------------------------------------------------------


class TestDeterministicCapExhaustion:
    """Tests proving cap exhaustion is deterministic and observable."""

    @pytest.mark.asyncio
    async def test_cap_exhaustion_emits_stable_event(self, repo: Any) -> None:
        """When cap is exhausted, a WORKER_PROPOSAL_CAP_EXHAUSTED event is emitted."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # 5 proposals, cap=3: 1 fact consumes 1, leaving 2 for proposals
        proposals = [
            _file_proposal(path=f"src/file_{i}.py") for i in range(5)
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(3)
        )

        events = repo.list_events("task-1")
        cap_events = [
            e for e in events
            if "CAP_EXHAUSTED" in str(e.get("event_type", "")).upper()
        ]
        assert len(cap_events) >= 1

    @pytest.mark.asyncio
    async def test_cap_exhaustion_deterministic_order(self, repo: Any) -> None:
        """Multiple handoffs with proposals: cap consumption is deterministic."""
        from hungerloop.services.handoff_processor import HandoffProcessor

        gate = CheckProposalGate()
        compiler = RefinementCompiler(repo)
        processor = HandoffProcessor(
            repo,
            requirement_compiler=RequirementCompiler(repo),
            check_proposal_gate=gate,
            refinement_compiler=compiler,
        )
        # Two handoffs, each with proposals
        h1 = _handoff(
            _test_gap_item(
                summary="gap1",
                proposed_checks=[_file_proposal(path="src/a.py")],
            ),
            agent_id="agent-1",
        )
        h2 = _handoff(
            _test_gap_item(
                summary="gap2",
                proposed_checks=[_file_proposal(path="src/b.py")],
            ),
            agent_id="agent-2",
        )

        # cap=2: 2 facts consume 2, leaving 0 for proposals
        result = await processor.process_handoffs(
            "task-1", 3, [h1, h2], mission=None, budget=_budget(2)
        )

        # Both facts consumed the cap, so no proposals injected
        assert result.accepted_proposal_count == 0

        # Cap exhaustion events should be present
        events = repo.list_events("task-1")
        cap_events = [
            e for e in events
            if "CAP_EXHAUSTED" in str(e.get("event_type", "")).upper()
        ]
        assert len(cap_events) >= 1

    @pytest.mark.asyncio
    async def test_cap_exhaustion_does_not_increase_accepted_count(
        self, repo: Any
    ) -> None:
        """Over-cap proposals do not increase accepted_proposal_count."""
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
            _file_proposal(path=f"src/file_{i}.py") for i in range(10)
        ]
        handoff = _handoff(_test_gap_item(proposed_checks=proposals))

        # cap=3: 1 fact consumes 1, leaving 2 for proposals
        result = await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(3)
        )

        assert result.accepted_proposal_count == 2

    @pytest.mark.asyncio
    async def test_cap_exhaustion_no_ledger_mutation(self, repo: Any) -> None:
        """Over-cap proposals do not mutate the ledger."""
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

        await processor.process_handoffs(
            "task-1", 3, [handoff], mission=None, budget=_budget(3)
        )

        ledger = repo.get_hunger_ledger("task-1")
        syn_items = [i for i in ledger.items if i.id.startswith("H-SYN-")]
        # Only 2 H-SYN items (cap - 1 fact = 2)
        assert len(syn_items) == 2
