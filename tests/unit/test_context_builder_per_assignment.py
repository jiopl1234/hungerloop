from __future__ import annotations

from typing import Literal

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.handoff import HandoffProcessingResult
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.planning import Assignment, BudgetAllocation
from hungerloop.models.worker import WorkerHandoff
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.context_builder import MAX_HISTORY_CHARS, ContextBuilder


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


def _repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Per-assignment context")
    return repo


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


def _seed_item(repo: InMemoryRepository, item_id: str) -> None:
    repo.save_hunger_item(
        HungerItem(
            id=item_id,
            title=f"Item {item_id}",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": f"{item_id}.txt"},
                    description=f"{item_id}.txt exists",
                )
            ],
        )
    )


def _assignment(
    assignment_id: str,
    *,
    hunger_ids: list[str],
    feature_ids: list[str],
) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        agent_id="execution_worker_v1",
        mission="# Mission Title\n\nImplement feature-specific context.",
        target_hunger_item_ids=hunger_ids,
        target_feature_ids=feature_ids,
        allowed_tools=["read_file"],
    )


def _handoff(
    assignment_id: str,
    *,
    loop_id: int,
    summary: str,
    error_type: str | None = None,
) -> WorkerHandoff:
    return WorkerHandoff(
        agent_id="execution_worker_v1",
        task_id="task-1",
        loop_id=loop_id,
        assignment_id=assignment_id,
        summary=summary,
        error=f"{error_type} error" if error_type else None,
        error_type=error_type,
    )


def _build_pack(
    repo: InMemoryRepository,
    assignment: Assignment,
    *,
    loop_id: int = 4,
) -> ContextPack:
    return ContextBuilder(
        repo=repo,
        workspace_reader=StaticWorkspaceReader(),
    ).build_for_agent(
        assignment,
        task_id="task-1",
        loop_id=loop_id,
        budget=_budget(),
        output_schema_name="default",
    )


def test_context_pack_target_feature_ids_default() -> None:
    pack = ContextPack(
        task_id="task-1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="mission",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="candidates/loop_001",
        budget=_budget(),
    )

    assert pack.target_feature_ids == []


def test_context_pack_fields_match_assignment() -> None:
    repo = _repo()
    _seed_item(repo, "H-1")
    assignment = _assignment(
        "ASGN-task-1-4-0",
        hunger_ids=["H-1"],
        feature_ids=["F-1"],
    )

    pack = _build_pack(repo, assignment)

    assert pack.agent_id == "execution_worker_v1"
    assert pack.mission == "# Mission Title\n\nImplement feature-specific context."
    assert pack.target_hunger_item_ids == ["H-1"]
    assert pack.target_feature_ids == ["F-1"]
    assert pack.allowed_tools == ["read_file"]
    assert pack.candidate_workspace_ref == "candidates/loop_004"


def test_prior_handoff_summary_union_in_loop() -> None:
    repo = _repo()
    _seed_item(repo, "H-2")
    repo.save_handoff_processing_result(
        "task-1",
        HandoffProcessingResult(
            prior_handoff_summary="Cross-loop: preserve model-config findings."
        ),
    )
    repo.save_worker_handoff(
        _handoff(
            "ASGN-task-1-4-0",
            loop_id=4,
            summary="Upstream assignment completed API wiring.",
        )
    )
    repo.save_worker_handoff(
        _handoff(
            "ASGN-task-1-4-1",
            loop_id=4,
            summary="Current retry failure should not leak.",
            error_type="timeout",
        )
    )
    repo.save_worker_handoff(
        _handoff(
            "ASGN-task-1-3-0",
            loop_id=3,
            summary="Prior loop raw handoff should not be duplicated.",
        )
    )
    assignment = _assignment(
        "ASGN-task-1-4-1",
        hunger_ids=["H-2"],
        feature_ids=["F-2"],
    )

    pack = _build_pack(repo, assignment)

    assert "Cross-loop: preserve model-config findings." in pack.prior_handoff_summary
    assert "Upstream assignment completed API wiring." in pack.prior_handoff_summary
    assert "Current retry failure should not leak." not in pack.prior_handoff_summary
    assert "Prior loop raw handoff should not be duplicated." not in (
        pack.prior_handoff_summary
    )
    assert len(pack.prior_handoff_summary) <= MAX_HISTORY_CHARS


def test_prior_handoff_summary_cap_applies_across_union() -> None:
    repo = _repo()
    _seed_item(repo, "H-3")
    repo.save_handoff_processing_result(
        "task-1",
        HandoffProcessingResult(
            prior_handoff_summary="OLDEST-CROSS-LOOP " + ("O" * 780)
        ),
    )
    for index in range(5):
        repo.save_worker_handoff(
            _handoff(
                f"ASGN-task-1-4-{index}",
                loop_id=4,
                summary=f"NEW-UPSTREAM-{index} " + ("N" * 520),
            )
        )
    assignment = _assignment(
        "ASGN-task-1-4-5",
        hunger_ids=["H-3"],
        feature_ids=["F-3"],
    )

    pack = _build_pack(repo, assignment)

    assert "NEW-UPSTREAM-4" in pack.prior_handoff_summary
    assert "OLDEST-CROSS-LOOP" not in pack.prior_handoff_summary
    assert len(pack.prior_handoff_summary) <= MAX_HISTORY_CHARS
