"""Read-path purity tests for ContextBuilder."""
from __future__ import annotations

from unittest.mock import MagicMock

from hungerloop.models.enums import AcceptanceCheckType, LoopPhase
from hungerloop.models.hunger import AcceptanceCheck, HungerItem
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.context_builder import ContextBuilder
from hungerloop.services.workspace_reader import WorkspaceReader


def test_context_builder_does_not_call_repo_write_apis(
    fake_workspace_reader: WorkspaceReader,
) -> None:
    repo = MagicMock()
    repo.get_best_state.return_value = None
    repo.get_last_worker_result.return_value = None
    repo.list_loop_traces.return_value = []
    repo.list_successful_tool_call_evidence.return_value = []
    repo.get_hunger_items.return_value = [
        HungerItem(
            id="H-001",
            title="deliverable",
            acceptance_checks=[
                AcceptanceCheck(
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    params={"path": "hello.txt"},
                    description="hello.txt exists",
                )
            ],
        )
    ]

    ContextBuilder(repo=repo, workspace_reader=fake_workspace_reader).build_for_agent(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="m",
        target_hunger_item_ids=["H-001"],
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
        allowed_tools=["write_file"],
        output_schema_name="default",
        candidate_workspace_ref="candidates/loop_001",
    )

    for call in repo.method_calls:
        assert not call[0].startswith(("save_", "append_", "delete_", "update_"))
