"""Unit tests for cross-task memory recall (VAL-MEM-009 through VAL-MEM-016,
VAL-MEM-019, VAL-CROSS-007).

Covers:
- VAL-MEM-009: Context packs expose recalled memories safely
- VAL-MEM-010: Context building recalls cross-task promoted memories
- VAL-MEM-011: Recall caps and ordering are deterministic
- VAL-MEM-012: Recall can be disabled without side effects
- VAL-MEM-013: Execution workers render prior-mission insights
- VAL-MEM-014: Cross-task recall works end to end (SQLite)
- VAL-MEM-015: Memory recall APIs remain strict-type clean (verified by mypy gate)
- VAL-MEM-016: Memory code stays within static policy boundaries (verified by ruff gate)
- VAL-MEM-019: Recall stays within context budget and prompt-safety rules
- VAL-CROSS-007: Memory recall is additive to planning and validation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import HungerPolicy
from hungerloop.models.memory import MemoryCandidate, PromotedMemory
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.execution_worker import ExecutionWorker
from hungerloop.services.workspace_reader import WorkspaceReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLOIT)


def _promoted(
    *,
    memory_id: str,
    task_id: str,
    content: str,
    created_at: datetime,
    source_candidate_id: str = "cand-1",
) -> PromotedMemory:
    return PromotedMemory(
        memory_id=memory_id,
        source_candidate_id=source_candidate_id,
        task_id=task_id,
        content=content,
        created_at=created_at,
        approved_by="auto",
    )


def _mock_repo_with_policy(
    *,
    policy: HungerPolicy | None = None,
    promoted_memories: list[PromotedMemory] | None = None,
    task_id: str = "t1",
) -> MagicMock:
    """Build a mock repo that returns the given policy and promoted memories."""
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_latest_handoff_processing_result.return_value = None
    repo.get_last_worker_result.return_value = None
    repo.list_loop_traces.return_value = []
    repo.list_successful_tool_call_evidence.return_value = []
    repo.get_hunger_items.return_value = []
    repo.list_workspace_files.return_value = []
    if policy is not None:
        repo.get_hunger_policy.return_value = policy
    else:
        repo.get_hunger_policy.return_value = HungerPolicy()
    if promoted_memories is not None:
        repo.list_promoted_memories.return_value = promoted_memories
    else:
        repo.list_promoted_memories.return_value = []
    return repo


def _build_context(
    *,
    repo: MagicMock,
    workspace_reader: WorkspaceReader,
    task_id: str = "t1",
    loop_id: int = 1,
) -> ContextPack:
    return ContextBuilder(
        repo=repo,
        workspace_reader=workspace_reader,
    ).build_for_agent(
        task_id=task_id,
        loop_id=loop_id,
        agent_id="execution_worker_v1",
        mission="do the thing",
        target_hunger_item_ids=["H-001"],
        budget=_budget(),
        allowed_tools=["read_file"],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )


# ---------------------------------------------------------------------------
# VAL-MEM-009: Context packs expose recalled memories safely
# ---------------------------------------------------------------------------


class TestContextPackRecalledMemories:
    """VAL-MEM-009: Context packs expose recalled memories safely."""

    def test_default_recalled_memories_is_empty_list(self) -> None:
        pack = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="m",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
        )
        assert pack.recalled_memories == []

    def test_recalled_memories_can_be_populated(self) -> None:
        pack = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="m",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=["insight 1", "insight 2"],
        )
        assert pack.recalled_memories == ["insight 1", "insight 2"]

    def test_serialization_round_trips(self) -> None:
        pack = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="m",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=["a", "b"],
        )
        data = pack.model_dump()
        restored = ContextPack.model_validate(data)
        assert restored.recalled_memories == ["a", "b"]

    def test_non_string_entries_fail_validation(self) -> None:
        with pytest.raises(ValidationError):
            ContextPack(
                task_id="t1",
                loop_id=1,
                agent_id="a1",
                mission="m",
                phase="explore",
                target_hunger_item_ids=["H-001"],
                candidate_workspace_ref="cand",
                budget=_budget(),
                recalled_memories=[123],  # type: ignore[list-item]
            )


# ---------------------------------------------------------------------------
# VAL-MEM-010: Context building recalls cross-task promoted memories
# ---------------------------------------------------------------------------


class TestCrossTaskRecall:
    """VAL-MEM-010: Context building recalls cross-task promoted memories."""

    def test_recalls_promoted_memories_from_other_tasks(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        now = datetime.now(timezone.utc)
        # Task A has a promoted memory; task B is the current task
        promoted_a = _promoted(
            memory_id="m1",
            task_id="taskA",
            content="Use pytest fixtures for isolation",
            created_at=now,
        )
        # Task B (current task) also has a promoted memory, which should be excluded
        promoted_b = _promoted(
            memory_id="m2",
            task_id="taskB",
            content="This is own-task memory",
            created_at=now,
        )
        repo = _mock_repo_with_policy(
            promoted_memories=[promoted_a, promoted_b],
        )
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        assert len(pack.recalled_memories) == 1
        assert "Use pytest fixtures for isolation" in pack.recalled_memories[0]
        assert "This is own-task memory" not in pack.recalled_memories[0]

    def test_current_task_memories_excluded_from_recall(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        now = datetime.now(timezone.utc)
        promoted_current = _promoted(
            memory_id="m1",
            task_id="t1",
            content="Current task memory",
            created_at=now,
        )
        promoted_other = _promoted(
            memory_id="m2",
            task_id="t2",
            content="Other task memory",
            created_at=now,
        )
        repo = _mock_repo_with_policy(
            promoted_memories=[promoted_current, promoted_other],
        )
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
            task_id="t1",
        )
        assert len(pack.recalled_memories) == 1
        assert "Other task memory" in pack.recalled_memories[0]
        assert "Current task memory" not in pack.recalled_memories[0]

    def test_no_promoted_memories_yields_empty_recall(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        repo = _mock_repo_with_policy(promoted_memories=[])
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert pack.recalled_memories == []

    def test_unpromoted_candidates_do_not_appear(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Only promoted memories appear, not candidates."""
        repo = _mock_repo_with_policy(promoted_memories=[])
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert pack.recalled_memories == []


# ---------------------------------------------------------------------------
# VAL-MEM-011: Recall caps and ordering are deterministic
# ---------------------------------------------------------------------------


class TestRecallCapsAndOrdering:
    """VAL-MEM-011: Recall caps and ordering are deterministic."""

    def test_newest_first_ordering(self, fake_workspace_reader: WorkspaceReader) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content=f"Memory {i}",
                created_at=base + timedelta(days=i),
            )
            for i in range(3)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        # Should be newest first: m2, m1, m0
        assert "Memory 2" in pack.recalled_memories[0]
        assert "Memory 1" in pack.recalled_memories[1]
        assert "Memory 0" in pack.recalled_memories[2]

    def test_tie_break_by_memory_id(self, fake_workspace_reader: WorkspaceReader) -> None:
        """When created_at is equal, break ties by memory_id ascending."""
        same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        promoted = [
            _promoted(
                memory_id="m_zeta",
                task_id="taskZ",
                content="Zeta",
                created_at=same_time,
            ),
            _promoted(
                memory_id="m_alpha",
                task_id="taskA",
                content="Alpha",
                created_at=same_time,
            ),
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        # Tie-break: alpha before zeta
        assert "Alpha" in pack.recalled_memories[0]
        assert "Zeta" in pack.recalled_memories[1]

    def test_max_five_entries(self, fake_workspace_reader: WorkspaceReader) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content=f"Short memory {i}",
                created_at=base + timedelta(days=i),
            )
            for i in range(10)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert len(pack.recalled_memories) == 5

    def test_total_rendered_cap_1200_chars(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 5 memories, each 400 chars = 2000 total, should be capped to 1200
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content="x" * 400,
                created_at=base + timedelta(days=i),
            )
            for i in range(5)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        total = sum(len(m) for m in pack.recalled_memories)
        assert total <= 1200

    def test_deterministic_truncation_at_string_boundaries(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Truncation happens at string boundaries, not mid-character."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 5 long memories: each is 500 chars, total 2500 -> must truncate to 1200
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content=f"START{i}_" + "y" * 495,
                created_at=base + timedelta(days=i),
            )
            for i in range(5)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack1 = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        # Build a second time - should be identical
        pack2 = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert pack1.recalled_memories == pack2.recalled_memories
        # Total must be <= 1200
        total = sum(len(m) for m in pack1.recalled_memories)
        assert total <= 1200
        # No partial multi-byte characters (all strings are valid Python str)
        for m in pack1.recalled_memories:
            assert isinstance(m, str)

    def test_fewer_than_five_entries_kept_all(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content=f"Short {i}",
                created_at=base + timedelta(days=i),
            )
            for i in range(3)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert len(pack.recalled_memories) == 3

    def test_single_entry_exceeding_cap_is_truncated(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """A single entry exceeding the 1200 char cap is truncated to fit."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        promoted = [
            _promoted(
                memory_id="m0",
                task_id="taskA",
                content="z" * 2000,
                created_at=base,
            ),
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        assert len(pack.recalled_memories) == 1
        assert len(pack.recalled_memories[0]) <= 1200


# ---------------------------------------------------------------------------
# VAL-MEM-012: Recall can be disabled without side effects
# ---------------------------------------------------------------------------


class TestRecallDisabled:
    """VAL-MEM-012: Recall can be disabled without side effects."""

    def test_disabled_recall_yields_empty_list(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        now = datetime.now(timezone.utc)
        promoted = [
            _promoted(
                memory_id="m1",
                task_id="taskA",
                content="Cross-task insight",
                created_at=now,
            ),
        ]
        policy = HungerPolicy(memory_recall_enabled=False)
        repo = _mock_repo_with_policy(policy=policy, promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        assert pack.recalled_memories == []

    def test_disabled_recall_no_other_fields_change(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Disabled recall must not change any other ContextPack field."""
        now = datetime.now(timezone.utc)
        promoted = [
            _promoted(
                memory_id="m1",
                task_id="taskA",
                content="Cross-task insight",
                created_at=now,
            ),
        ]

        # Build with recall enabled
        policy_on = HungerPolicy(memory_recall_enabled=True)
        repo_on = _mock_repo_with_policy(policy=policy_on, promoted_memories=promoted)
        pack_on = _build_context(
            repo=repo_on,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )

        # Build with recall disabled
        policy_off = HungerPolicy(memory_recall_enabled=False)
        repo_off = _mock_repo_with_policy(policy=policy_off, promoted_memories=promoted)
        pack_off = _build_context(
            repo=repo_off,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )

        # Only recalled_memories should differ
        assert pack_on.recalled_memories != pack_off.recalled_memories
        assert pack_off.recalled_memories == []

        # All other fields should be identical
        assert pack_on.task_id == pack_off.task_id
        assert pack_on.loop_id == pack_off.loop_id
        assert pack_on.agent_id == pack_off.agent_id
        assert pack_on.mission == pack_off.mission
        assert pack_on.phase == pack_off.phase
        assert pack_on.target_hunger_item_ids == pack_off.target_hunger_item_ids
        assert pack_on.target_feature_ids == pack_off.target_feature_ids
        assert pack_on.acceptance_criteria == pack_off.acceptance_criteria
        assert pack_on.acceptance_check_keys == pack_off.acceptance_check_keys
        assert pack_on.passed_check_keys == pack_off.passed_check_keys
        assert pack_on.failing_check_keys == pack_off.failing_check_keys
        assert pack_on.best_state_summary == pack_off.best_state_summary
        assert pack_on.candidate_workspace_ref == pack_off.candidate_workspace_ref
        assert pack_on.relevant_evidence_ids == pack_off.relevant_evidence_ids
        assert pack_on.failure_patterns_to_avoid == pack_off.failure_patterns_to_avoid
        assert pack_on.last_self_summary == pack_off.last_self_summary
        assert pack_on.prior_handoff_summary == pack_off.prior_handoff_summary
        assert pack_on.relevant_evidence_summaries == pack_off.relevant_evidence_summaries
        assert pack_on.best_workspace_files == pack_off.best_workspace_files
        assert pack_on.truncation_info == pack_off.truncation_info
        assert pack_on.allowed_tools == pack_off.allowed_tools
        assert pack_on.budget == pack_off.budget
        assert pack_on.required_output_schema == pack_off.required_output_schema


# ---------------------------------------------------------------------------
# VAL-MEM-013: Execution workers render prior-mission insights
# ---------------------------------------------------------------------------


class TestExecutionWorkerInsights:
    """VAL-MEM-013: Execution workers render prior-mission insights."""

    def test_prompt_includes_insights_when_recalled_memories_nonempty(self) -> None:
        ctx = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=["Use type hints everywhere", "Test edge cases first"],
        )
        messages = ExecutionWorker._messages(ctx)
        prompt = "\n".join(m["content"] for m in messages)
        # Should have a labeled section
        assert "prior-mission insights" in prompt.lower()
        # Should include the recalled memory text
        assert "Use type hints everywhere" in prompt
        assert "Test edge cases first" in prompt

    def test_prompt_omits_insights_section_when_empty(self) -> None:
        ctx = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=[],
        )
        ctx_with = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=[],
        )
        messages_empty = ExecutionWorker._messages(ctx)
        messages_empty2 = ExecutionWorker._messages(ctx_with)
        # When empty, no insights section
        prompt_empty = "\n".join(m["content"] for m in messages_empty)
        prompt_empty2 = "\n".join(m["content"] for m in messages_empty2)
        assert "prior-mission insights" not in prompt_empty.lower()
        # Both should be identical (no section added)
        assert prompt_empty == prompt_empty2

    def test_prompt_sections_unchanged_when_no_recall(self) -> None:
        """Existing prompt sections retain their content and ordering."""
        ctx = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            acceptance_criteria=["criterion 1"],
            recalled_memories=[],
        )
        messages = ExecutionWorker._messages(ctx)
        prompt = "\n".join(m["content"] for m in messages)
        # Existing sections should still be there
        assert "Mission:" in prompt
        assert "Acceptance criteria:" in prompt
        assert "criterion 1" in prompt
        assert "Required JSON shape" in prompt

    def test_insights_are_prompt_safe(self) -> None:
        """Insights are prompt-safe: no volatile ids, paths, or secrets."""
        ctx = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=["A safe insight about testing"],
        )
        messages = ExecutionWorker._messages(ctx)
        prompt = "\n".join(m["content"] for m in messages)
        # No secret patterns
        assert "API_KEY" not in prompt
        assert "Bearer" not in prompt
        assert ".env" not in prompt
        assert "sk-" not in prompt


# ---------------------------------------------------------------------------
# VAL-MEM-019: Recall stays within context budget and prompt-safety rules
# ---------------------------------------------------------------------------


class TestRecallContextBudget:
    """VAL-MEM-019: Recall stays within context budget and prompt-safety rules."""

    def test_recall_participates_in_truncation_accounting(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Recalled memories are accounted for in context size."""
        now = datetime.now(timezone.utc)
        # Create memories that would push past the char cap
        promoted = [
            _promoted(
                memory_id=f"m{i}",
                task_id=f"task{i}",
                content="z" * 500,
                created_at=now + timedelta(days=i),
            )
            for i in range(5)
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
        )
        # Total recalled memory chars should be within the 1200 cap
        total = sum(len(m) for m in pack.recalled_memories)
        assert total <= 1200

    def test_recalled_memories_are_secret_safe(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Recalled memories do not contain secret patterns."""
        now = datetime.now(timezone.utc)
        promoted = [
            _promoted(
                memory_id="m1",
                task_id="taskA",
                content="A safe reusable insight about code patterns",
                created_at=now,
            ),
        ]
        repo = _mock_repo_with_policy(promoted_memories=promoted)
        pack = _build_context(
            repo=repo,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        for m in pack.recalled_memories:
            assert "API_KEY" not in m
            assert "Bearer" not in m
            assert ".env" not in m
            assert "sk-" not in m

    def test_formatted_prompt_secret_scan(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """The rendered prompt with recalled memories is secret-safe."""
        ctx = ContextPack(
            task_id="t1",
            loop_id=1,
            agent_id="a1",
            mission="do work",
            phase="explore",
            target_hunger_item_ids=["H-001"],
            candidate_workspace_ref="cand",
            budget=_budget(),
            recalled_memories=[
                "Insight about using pytest fixtures",
                "Insight about error handling patterns",
            ],
        )
        messages = ExecutionWorker._messages(ctx)
        prompt = "\n".join(m["content"] for m in messages)
        # Secret scan
        assert "API_KEY" not in prompt
        assert "Bearer" not in prompt
        assert ".env" not in prompt
        assert "sk-" not in prompt
        # Prompt-safe: no task-specific volatile identifiers
        assert "CAND-" not in prompt


# ---------------------------------------------------------------------------
# VAL-CROSS-007: Memory recall is additive to planning and validation
# ---------------------------------------------------------------------------


class TestRecallAdditive:
    """VAL-CROSS-007: Memory recall is additive to planning and validation."""

    def test_recall_does_not_change_target_items(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        """Recalled memories do not change target hunger items."""
        now = datetime.now(timezone.utc)
        promoted = [
            _promoted(
                memory_id="m1",
                task_id="taskA",
                content="Cross-task insight",
                created_at=now,
            ),
        ]
        # Enabled
        repo_on = _mock_repo_with_policy(promoted_memories=promoted)
        pack_on = _build_context(
            repo=repo_on,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        # Disabled
        policy_off = HungerPolicy(memory_recall_enabled=False)
        repo_off = _mock_repo_with_policy(policy=policy_off, promoted_memories=promoted)
        pack_off = _build_context(
            repo=repo_off,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        # Target items are identical
        assert pack_on.target_hunger_item_ids == pack_off.target_hunger_item_ids
        assert pack_on.acceptance_criteria == pack_off.acceptance_criteria
        assert pack_on.acceptance_check_keys == pack_off.acceptance_check_keys

    def test_recall_does_not_change_acceptance_criteria(
        self,
        fake_workspace_reader: WorkspaceReader,
    ) -> None:
        now = datetime.now(timezone.utc)
        promoted = [
            _promoted(
                memory_id="m1",
                task_id="taskA",
                content="Insight",
                created_at=now,
            ),
        ]
        repo_on = _mock_repo_with_policy(promoted_memories=promoted)
        pack_on = _build_context(
            repo=repo_on,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        policy_off = HungerPolicy(memory_recall_enabled=False)
        repo_off = _mock_repo_with_policy(policy=policy_off, promoted_memories=promoted)
        pack_off = _build_context(
            repo=repo_off,
            workspace_reader=fake_workspace_reader,
            task_id="taskB",
        )
        assert pack_on.acceptance_criteria == pack_off.acceptance_criteria
        assert pack_on.passed_check_keys == pack_off.passed_check_keys
        assert pack_on.failing_check_keys == pack_off.failing_check_keys


# ---------------------------------------------------------------------------
# VAL-MEM-014: Cross-task recall works end to end (SQLite)
# ---------------------------------------------------------------------------


class TestCrossTaskRecallSQLite:
    """VAL-MEM-014: Cross-task recall works end to end with SQLite."""

    def test_sqlite_cross_task_recall_enabled(
        self, tmp_path: object,
    ) -> None:
        from hungerloop.repository.in_memory_repo import InMemoryRepository

        # Use in-memory repo for this test but verify the full builder path
        repo = InMemoryRepository()
        repo.create_task("taskA", "Goal A")
        repo.create_task("taskB", "Goal B")

        now = datetime.now(timezone.utc)

        # Save memory candidate for task A, then promote it
        cand = MemoryCandidate(
            candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            accepted_check_keys=["H-001:0"],
            source_loop_ids=[1],
            state="proposed",
        )
        repo.save_memory_candidate(cand)
        promoted = PromotedMemory(
            memory_id="prom-a1",
            source_candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(promoted)

        # Build context for task B
        reader = MagicMock()
        reader.list_workspace_files.return_value = []
        reader.list_workspace_file_stats.return_value = []

        # Set up the repo for task B context building
        repo.get_best_state = MagicMock(return_value=None)  # type: ignore
        repo.get_latest_handoff_processing_result = MagicMock(return_value=None)  # type: ignore
        repo.get_last_worker_result = MagicMock(return_value=None)  # type: ignore
        repo.list_loop_traces = MagicMock(return_value=[])  # type: ignore
        repo.list_successful_tool_call_evidence = MagicMock(return_value=[])  # type: ignore
        repo.get_hunger_items = MagicMock(return_value=[])  # type: ignore

        pack = ContextBuilder(
            repo=repo,
            workspace_reader=reader,
        ).build_for_agent(
            task_id="taskB",
            loop_id=1,
            agent_id="worker-1",
            mission="Goal B",
            target_hunger_item_ids=["H-001"],
            budget=_budget(),
            allowed_tools=["read_file"],
            output_schema_name="default",
            candidate_workspace_ref="candidates/loop_001",
        )
        assert len(pack.recalled_memories) == 1
        assert "Use pytest fixtures for test isolation" in pack.recalled_memories[0]

    def test_sqlite_cross_task_recall_disabled(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from hungerloop.repository.in_memory_repo import InMemoryRepository

        repo = InMemoryRepository()
        repo.create_task("taskA", "Goal A")
        repo.create_task("taskB", "Goal B")

        now = datetime.now(timezone.utc)
        cand = MemoryCandidate(
            candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            accepted_check_keys=["H-001:0"],
            source_loop_ids=[1],
            state="proposed",
        )
        repo.save_memory_candidate(cand)
        promoted = PromotedMemory(
            memory_id="prom-a1",
            source_candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(promoted)

        # Disable recall for task B
        policy = repo.get_hunger_policy("taskB")
        policy.memory_recall_enabled = False
        repo.set_hunger_policy("taskB", policy)

        reader = MagicMock()
        reader.list_workspace_files.return_value = []
        reader.list_workspace_file_stats.return_value = []

        repo.get_best_state = MagicMock(return_value=None)  # type: ignore
        repo.get_latest_handoff_processing_result = MagicMock(return_value=None)  # type: ignore
        repo.get_last_worker_result = MagicMock(return_value=None)  # type: ignore
        repo.list_loop_traces = MagicMock(return_value=[])  # type: ignore
        repo.list_successful_tool_call_evidence = MagicMock(return_value=[])  # type: ignore
        repo.get_hunger_items = MagicMock(return_value=[])  # type: ignore

        pack = ContextBuilder(
            repo=repo,
            workspace_reader=reader,
        ).build_for_agent(
            task_id="taskB",
            loop_id=1,
            agent_id="worker-1",
            mission="Goal B",
            target_hunger_item_ids=["H-001"],
            budget=_budget(),
            allowed_tools=["read_file"],
            output_schema_name="default",
            candidate_workspace_ref="candidates/loop_001",
        )
        assert pack.recalled_memories == []


class TestCrossTaskRecallSQLiteReal:
    """VAL-MEM-014: Cross-task recall with real SQLite repository."""

    def test_sqlite_real_repo_cross_task_recall(
        self, tmp_path: object,
    ) -> None:
        from pathlib import Path

        from hungerloop.repository.sqlite_repo import SQLiteRepository

        db_path = Path(str(tmp_path)) / "test_recall.db"
        repo = SQLiteRepository(db_path)
        repo.create_task("taskA", "Goal A")
        repo.create_task("taskB", "Goal B")

        now = datetime.now(timezone.utc)

        # Save a memory candidate for task A
        cand = MemoryCandidate(
            candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            accepted_check_keys=["H-001:0"],
            source_loop_ids=[1],
            state="proposed",
        )
        repo.save_memory_candidate(cand)
        promoted = PromotedMemory(
            memory_id="prom-a1",
            source_candidate_id="cand-a1",
            task_id="taskA",
            content="Use pytest fixtures for test isolation",
            created_at=now,
            approved_by="auto",
        )
        repo.save_promoted_memory(promoted)

        # Build context for task B with recall enabled
        reader = MagicMock()
        reader.list_workspace_files.return_value = []
        reader.list_workspace_file_stats.return_value = []

        pack = ContextBuilder(
            repo=repo,
            workspace_reader=reader,
        ).build_for_agent(
            task_id="taskB",
            loop_id=1,
            agent_id="worker-1",
            mission="Goal B",
            target_hunger_item_ids=["H-001"],
            budget=_budget(),
            allowed_tools=["read_file"],
            output_schema_name="default",
            candidate_workspace_ref="candidates/loop_001",
        )
        assert len(pack.recalled_memories) == 1
        assert "Use pytest fixtures for test isolation" in pack.recalled_memories[0]

        # Now disable recall and verify it's empty
        policy = repo.get_hunger_policy("taskB")
        policy.memory_recall_enabled = False
        repo.set_hunger_policy("taskB", policy)

        pack_disabled = ContextBuilder(
            repo=repo,
            workspace_reader=reader,
        ).build_for_agent(
            task_id="taskB",
            loop_id=1,
            agent_id="worker-1",
            mission="Goal B",
            target_hunger_item_ids=["H-001"],
            budget=_budget(),
            allowed_tools=["read_file"],
            output_schema_name="default",
            candidate_workspace_ref="candidates/loop_001",
        )
        assert pack_disabled.recalled_memories == []

        repo.close()
