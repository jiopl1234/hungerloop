from __future__ import annotations

from typing import Literal

import pytest

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.context_builder import ContextBuilder, _apply_history_cap
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.prior_loop_context import render_prior_loop_context_block
from hungerloop.services.requirement_compiler import RequirementCompiler


class StaticWorkspaceReader:
    def list_workspace_files(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[str]:
        del task_id, ref, loop_id
        return []


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


def _repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Context builder handoff integration")
    return repo


def _seed_item(repo: InMemoryRepository) -> None:
    repo.save_hunger_item(
        HungerItem(
            id="H-001",
            title="Core deliverable",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "hello.txt"},
                    description="hello.txt exists",
                )
            ],
        )
    )


def _handoff(*items: HandoffItem) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=2,
        summary="Completed a partial pass and left structured handoff notes.",
        handoff_items=list(items),
    )


def _build_pack(repo: InMemoryRepository, *, loop_id: int = 3) -> ContextPack:
    _seed_item(repo)
    return ContextBuilder(
        repo=repo,
        workspace_reader=StaticWorkspaceReader(),
    ).build_for_agent(
        task_id="task-1",
        loop_id=loop_id,
        agent_id="execution_worker_v1",
        mission="Make progress on H-001",
        target_hunger_item_ids=["H-001"],
        budget=_budget(),
        allowed_tools=["read_file", "write_file"],
        output_schema_name="default",
        candidate_workspace_ref=f"candidates/loop_{loop_id:03d}",
    )


def test_context_pack_prior_handoff_summary_default() -> None:
    pack = ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="mission",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="candidates/loop_001",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert pack.prior_handoff_summary == ""


@pytest.mark.asyncio
async def test_build_for_agent_uses_latest_cached_handoff_summary() -> None:
    repo = _repo()
    result = await HandoffProcessor(
        repo,
        requirement_compiler=RequirementCompiler(repo),
    ).process_handoffs(
        "task-1",
        2,
        [
            _handoff(
                HandoffItem(
                    item_type="critical_context",
                    summary="DB schema mismatch",
                ),
                HandoffItem(
                    item_type="follow_up",
                    summary="rerun the targeted validator",
                ),
            )
        ],
        mission=None,
        budget=_budget(),
    )
    pack = _build_pack(repo)

    assert pack.prior_handoff_summary == result.prior_handoff_summary
    assert pack.prior_handoff_summary.startswith("[CRITICAL] DB schema mismatch")
    assert "Follow-up: rerun the targeted validator" in pack.prior_handoff_summary


def test_handoff_takes_precedence_over_self_summary() -> None:
    (
        prior_handoff_summary,
        last_self_summary,
        _evidence_ids,
        _evidence_lines,
        _failure_lines,
        _truncation_info,
    ) = _apply_history_cap(
        loop_id=2,
        prior_handoff_summary="H" * 1500,
        last_summary="S" * 1500,
        best_summary=None,
        best_files=[],
        evidence_ids=[],
        evidence_lines=[],
        failure_lines=[],
        best_summary_truncated=False,
    )

    assert len(prior_handoff_summary) == 1500
    assert last_self_summary is not None
    assert len(last_self_summary) <= 500


def test_render_order() -> None:
    rendered = render_prior_loop_context_block(
        loop_id=3,
        last_self_summary="Tried the migration path once.",
        prior_handoff_summary="Follow-up: verify the rollback fixture.",
        best_state_summary="best state summary",
        best_workspace_files=["src/hungerloop/services/context_builder.py"],
        failure_patterns_to_avoid=[],
        relevant_evidence_summaries=[],
    )

    last_attempt_index = rendered.index("- last attempt summary:")
    handoff_index = rendered.index("- prior handoff summary:")
    best_state_index = rendered.index("- best state:")

    assert last_attempt_index < handoff_index < best_state_index


def test_rendering_with_only_handoff_summary_is_deterministic() -> None:
    kwargs = {
        "loop_id": 3,
        "last_self_summary": None,
        "prior_handoff_summary": "Follow-up: reuse the generated fixture.",
        "best_state_summary": None,
        "best_workspace_files": [],
        "failure_patterns_to_avoid": [],
        "relevant_evidence_summaries": [],
    }

    rendered_once = render_prior_loop_context_block(**kwargs)
    rendered_twice = render_prior_loop_context_block(**kwargs)

    assert rendered_once == rendered_twice
    assert "- prior handoff summary:" in rendered_once
