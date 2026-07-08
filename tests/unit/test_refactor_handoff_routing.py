"""Tests for refactor_proposal handoff routing (VAL-REF-014, VAL-REF-015).

Covers:
- Handoff processing calls transaction open/close only for refactor_proposal items
- Open proposals require declared keys and rationale
- Close proposals target the current task's open transaction
- Invalid actions or malformed payloads produce rejected results
- Routing never writes ledger items or mission artifacts
- Disabled policy prevents routing
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hungerloop.models.blackboard import BestState
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.refactor import RefactorProposalPayload
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.refactor_transaction_manager import RefactorTransactionManager
from hungerloop.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_task(
    repo: InMemoryRepository,
    task_id: str = "task-1",
    accepted_keys: list[str] | None = None,
    policy: HungerPolicy | None = None,
) -> BestState:
    accepted_keys = accepted_keys or ["H-001:0", "H-002:0"]
    repo.create_task(task_id, "test goal")
    if policy is None:
        policy = HungerPolicy(refactor_transactions_enabled=True)
    repo.set_hunger_policy(task_id, policy)
    best = BestState(
        task_id=task_id,
        state_id="BEST-001",
        summary="baseline",
        accepted_check_keys=accepted_keys,
        updated_at_loop=5,
    )
    repo.save_best_state(best)
    return best


def _write_best_files(ws: WorkspaceManager, task_id: str, files: dict[str, str]) -> None:
    best_dir = ws.best_files_dir(task_id)
    best_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        full = best_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def _make_handoff(
    task_id: str = "task-1",
    loop_id: int = 10,
    agent_id: str = "worker-1",
    items: list[HandoffItem] | None = None,
) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id=agent_id,
        task_id=task_id,
        loop_id=loop_id,
        handoff_items=items or [],
    )


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path / "workspace")


@pytest.fixture
def txn_manager(repo: InMemoryRepository, ws: WorkspaceManager) -> RefactorTransactionManager:
    return RefactorTransactionManager(repo=repo, workspace_manager=ws)


@pytest.fixture
def processor(
    repo: InMemoryRepository,
    ws: WorkspaceManager,
    txn_manager: RefactorTransactionManager,
) -> HandoffProcessor:
    return HandoffProcessor(
        repo=repo,
        refactor_transaction_manager=txn_manager,
    )


# ---------------------------------------------------------------------------
# VAL-REF-014: Refactor proposal handoffs route only to transaction handling
# ---------------------------------------------------------------------------


class TestRefactorProposalRouting:
    """Processing refactor_proposal items routes to transaction manager."""

    @pytest.mark.asyncio
    async def test_open_proposal_calls_transaction_manager(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="refactor module X",
            detail="Need to temporarily break H-001:0 while restructuring",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=["H-001:0"],
                rationale="restructuring module X",
            ),
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # Transaction should be opened
        txn = repo.get_open_refactor_transaction("task-1")
        assert txn is not None
        assert txn.declared_regression_keys == ["H-001:0"]

    @pytest.mark.asyncio
    async def test_close_proposal_calls_transaction_manager(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo, accepted_keys=["H-001:0", "H-002:0"])
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        # First open a transaction
        open_item = HandoffItem(
            item_type="refactor_proposal",
            summary="open refactor",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=["H-001:0"],
                rationale="restructuring",
            ),
        )
        await processor.process_handoffs(
            "task-1",
            10,
            [_make_handoff(items=[open_item])],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # Now close it with a successful best state
        new_best = BestState(
            task_id="task-1",
            state_id="BEST-002",
            summary="after refactor",
            accepted_check_keys=["H-001:0", "H-002:0", "H-003:0"],
            updated_at_loop=12,
        )
        repo.save_best_state(new_best)

        close_item = HandoffItem(
            item_type="refactor_proposal",
            summary="close refactor",
            refactor_proposal_payload=RefactorProposalPayload(
                action="close",
            ),
        )
        await processor.process_handoffs(
            "task-1",
            12,
            [_make_handoff(loop_id=12, items=[close_item])],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # Transaction should be closed
        assert repo.get_open_refactor_transaction("task-1") is None

    @pytest.mark.asyncio
    async def test_invalid_action_rejected(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="invalid action",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=[],
                rationale="no declared keys",
            ),
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # No transaction should be opened
        assert repo.get_open_refactor_transaction("task-1") is None

    @pytest.mark.asyncio
    async def test_no_payload_does_not_call_manager(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="missing payload",
            # refactor_proposal_payload is None
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # No transaction should be opened
        assert repo.get_open_refactor_transaction("task-1") is None


# ---------------------------------------------------------------------------
# VAL-REF-015: Routing is compiler-safe (no ledger or mission writes)
# ---------------------------------------------------------------------------


class TestRoutingCompilerSafe:
    """Processing refactor_proposal never writes ledger or mission artifacts."""

    @pytest.mark.asyncio
    async def test_no_hunger_ledger_writes(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        # Capture initial ledger
        initial_ledger = repo.get_hunger_ledger("task-1")
        initial_item_count = len(initial_ledger.items)

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="refactor",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=["H-001:0"],
                rationale="restructuring",
            ),
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # Ledger should be unchanged
        final_ledger = repo.get_hunger_ledger("task-1")
        assert len(final_ledger.items) == initial_item_count

    @pytest.mark.asyncio
    async def test_no_mission_artifact_writes(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
        processor: HandoffProcessor,
    ) -> None:
        _setup_task(repo)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="refactor",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=["H-001:0"],
                rationale="restructuring",
            ),
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # No mission should have been created
        assert repo.get_mission("task-1") is None


# ---------------------------------------------------------------------------
# VAL-REF-022: Disabled policy prevents routing
# ---------------------------------------------------------------------------


class TestDisabledPolicyRouting:
    """Disabled policy prevents refactor proposal routing."""

    @pytest.mark.asyncio
    async def test_disabled_policy_no_open(
        self,
        repo: InMemoryRepository,
        ws: WorkspaceManager,
    ) -> None:
        policy = HungerPolicy(refactor_transactions_enabled=False)
        _setup_task(repo, policy=policy)
        _write_best_files(ws, "task-1", {"file1.py": "content"})

        txn_manager = RefactorTransactionManager(repo=repo, workspace_manager=ws)
        processor = HandoffProcessor(
            repo=repo,
            refactor_transaction_manager=txn_manager,
        )

        item = HandoffItem(
            item_type="refactor_proposal",
            summary="refactor",
            refactor_proposal_payload=RefactorProposalPayload(
                action="open",
                declared_regression_keys=["H-001:0"],
                rationale="restructuring",
            ),
        )
        handoff = _make_handoff(items=[item])

        await processor.process_handoffs(
            "task-1",
            10,
            [handoff],
            mission=None,
            budget=MagicMock(max_new_items_per_loop=5),
        )

        # No transaction should be opened
        assert repo.get_open_refactor_transaction("task-1") is None
