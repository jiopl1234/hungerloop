"""Unit tests for ExecutionWorker (PRD §8.2 + §28.1)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.execution_worker import ExecutionWorker
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.tool_harness import ToolHarness
from hungerloop.services.tools import default_tool_registry


def _ctx(workspace_ref: str = "cand") -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="write a demo report",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref=workspace_ref,
        acceptance_criteria=["report.md exists"],
        allowed_tools=["read_file", "write_file", "patch_file", "run_shell"],
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )


def _build_worker(
    tmp_path: Path, model_client: DummyModelClient
) -> tuple[ExecutionWorker, InMemoryRepository, Path]:
    repo = InMemoryRepository()
    sandbox = SandboxRunner(repo)
    harness = ToolHarness(repo, default_tool_registry(sandbox), BudgetGuard())
    worker = ExecutionWorker(model_client, harness, repo)
    return worker, repo, tmp_path


async def test_execution_worker_dispatches_scripted_actions(
    tmp_path: Path,
) -> None:
    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# demo report\nok\n"},
        }
    ]
    worker, repo, workspace = _build_worker(
        tmp_path, DummyModelClient.with_actions(actions)
    )
    result = await worker.run(context=_ctx(), workspace_root=workspace)

    assert result.error is None
    assert result.summary == "scripted dummy response"
    assert (workspace / "report.md").read_text() == "# demo report\nok\n"
    assert len(result.evidence_ids) == 1  # one tool_call evidence row
    assert len(result.artifact_ids) == 1
    artifact = repo._artifacts[result.artifact_ids[0]]
    assert artifact.path == "report.md"


async def test_execution_worker_with_no_actions_returns_summary(
    tmp_path: Path,
) -> None:
    worker, _, workspace = _build_worker(tmp_path, DummyModelClient())
    result = await worker.run(context=_ctx(), workspace_root=workspace)
    assert result.error is None
    assert result.summary == "dummy fallback"
    assert result.evidence_ids == []
    assert result.artifact_ids == []


async def test_execution_worker_skips_malformed_action(tmp_path: Path) -> None:
    """Non-dict items in the actions list are skipped, not crashing."""
    actions: list[dict[str, object]] = [
        {
            "tool_name": "write_file",
            "args": {"path": "ok.txt", "content": "ok"},
        }
    ]
    bad_payload: list[object] = ["garbage", *actions]
    # Construct a dummy with a hand-rolled response containing the bad list.
    import json

    from hungerloop.models.usage import ModelUsage
    from hungerloop.services.model_client import ModelResponse
    body: dict[str, object] = {"summary": "mixed", "actions": bad_payload}
    client = DummyModelClient(
        [
            ModelResponse(
                content=json.dumps(body),
                json_data=body,
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            )
        ]
    )
    worker, _, workspace = _build_worker(tmp_path, client)
    result = await worker.run(context=_ctx(), workspace_root=workspace)
    assert result.error is None
    assert (workspace / "ok.txt").read_text() == "ok"


async def test_execution_worker_passes_retry_kwargs(tmp_path: Path) -> None:
    """Sanity-check the call site threads retry params from budget."""
    captured: dict[str, object] = {}

    class _SpyClient(DummyModelClient):
        async def complete_json(  # type: ignore[override]
            self,
            *,
            task_id: str,
            agent_id: str,
            messages: list[dict[str, str]],
            max_tokens: int,
            max_retries: int = 0,
            retry_base_delay_seconds: float = 1.0,
            retry_max_delay_seconds: float = 20.0,
        ):
            captured["max_retries"] = max_retries
            captured["retry_base"] = retry_base_delay_seconds
            captured["retry_max"] = retry_max_delay_seconds
            return await super().complete_json(
                task_id=task_id,
                agent_id=agent_id,
                messages=messages,
                max_tokens=max_tokens,
                max_retries=max_retries,
                retry_base_delay_seconds=retry_base_delay_seconds,
                retry_max_delay_seconds=retry_max_delay_seconds,
            )

    worker, _, workspace = _build_worker(tmp_path, _SpyClient())
    ctx = _ctx()
    await worker.run(context=ctx, workspace_root=workspace)
    assert captured["max_retries"] == ctx.budget.max_model_retries
    assert captured["retry_base"] == ctx.budget.retry_base_delay_seconds
    assert captured["retry_max"] == ctx.budget.retry_max_delay_seconds
