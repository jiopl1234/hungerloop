"""ExecutionWorker prior-loop prompt block tests."""
from __future__ import annotations

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.execution_worker import ExecutionWorker


def _ctx(**updates: object) -> ContextPack:
    data: dict[str, object] = {
        "task_id": "t1",
        "loop_id": 2,
        "agent_id": "execution_worker_v1",
        "mission": "m",
        "phase": LoopPhase.EXPLORE.value,
        "target_hunger_item_ids": ["H-001"],
        "acceptance_criteria": ["hello.txt exists"],
        "candidate_workspace_ref": "candidates/loop_002",
        "allowed_tools": ["write_file"],
        "budget": BudgetAllocation(phase=LoopPhase.EXPLORE),
    }
    data.update(updates)
    return ContextPack.model_validate(data)


def test_history_block_suppressed_when_empty() -> None:
    user = ExecutionWorker._messages(_ctx())[1]["content"]

    assert "Prior loop context" not in user


def test_history_block_renders_failure_summary_only() -> None:
    user = ExecutionWorker._messages(
        _ctx(
            last_self_summary="explore",
            failure_patterns_to_avoid=["loop 1: H-001:0 file_exists → False"],
        )
    )[1]["content"]

    assert user.count("Prior loop context") == 1
    assert "patterns to avoid" in user
    assert "loop 1: H-001:0" in user
    assert "successful actions already on record" not in user
    assert "files in best/:" not in user
    assert "prefer\npatch_file over a fresh write_file" in user


def test_history_block_renders_files_and_evidence() -> None:
    user = ExecutionWorker._messages(
        _ctx(
            best_workspace_files=["hello.txt"],
            relevant_evidence_summaries=["loop 2 tool_call write_file: wrote"],
        )
    )[1]["content"]

    assert "files in best/: hello.txt" in user
    assert "successful actions already on record" in user
    assert "loop 2 tool_call write_file: wrote" in user
    assert user.index("Prior loop context") < user.index("Required JSON shape example")
