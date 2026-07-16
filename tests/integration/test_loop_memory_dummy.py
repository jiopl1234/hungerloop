"""Synthetic cross-loop memory regression."""
from __future__ import annotations

import json
from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import AcceptanceCheckType, StopReason
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.usage import ModelUsage
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient, ModelResponse


class CapturingDummyClient(DummyModelClient):
    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    async def complete_json(self, **kwargs: object) -> ModelResponse:  # type: ignore[override]
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        self.prompts.append(messages[1]["content"])
        return await super().complete_json(**kwargs)  # type: ignore[arg-type]


def _response(summary: str, actions: list[dict[str, object]]) -> ModelResponse:
    payload: dict[str, object] = {"summary": summary, "actions": actions}
    return ModelResponse(
        content=json.dumps(payload),
        json_data=payload,
        usage=ModelUsage(input_tokens=1, output_tokens=1),
    )


def _seed(repo: InMemoryRepository) -> None:
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(max_total_cost_usd=10.0, max_total_tokens=100_000),
    )
    repo.get_hunger_clock("t1")
    item = HungerItem(
        id="H-001",
        title="hello",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "hello.txt"},
                description="hello.txt exists",
            )
        ],
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)


async def test_loop_memory_dummy_propagates_failure_to_next_prompt(
    tmp_path: Path,
) -> None:
    repo = InMemoryRepository()
    _seed(repo)
    dummy = CapturingDummyClient(
        [
            # Loop 1 writes + verifies a NON-acceptance file so the worker's
            # inner self-repair loop hands off after one model call, yet
            # FILE_EXISTS hello.txt still fails -> loop 1 is rejected and its
            # failure becomes prior-loop context for loop 2.
            _response(
                "explore",
                [
                    {
                        "tool_name": "write_file",
                        "args": {"path": "notes.txt", "content": "noted"},
                    },
                    {"tool_name": "run_shell", "args": {"argv": ["true"]}},
                ],
            ),
            _response(
                "write hello",
                [
                    {
                        "tool_name": "write_file",
                        "args": {"path": "hello.txt", "content": "hi"},
                    },
                    {"tool_name": "run_shell", "args": {"argv": ["true"]}},
                ],
            ),
        ]
    )
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=tmp_path,
        model_client=dummy,
        max_loops_safety_cap=3,
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")

    assert report.stop_reason is StopReason.DONE
    assert len(dummy.prompts) >= 2
    assert "Prior loop context" not in dummy.prompts[0]
    assert "Prior loop context" in dummy.prompts[1]
    assert "patterns to avoid" in dummy.prompts[1]
    assert "H-001:0 file_exists" in dummy.prompts[1]
    assert "last attempt summary: explore" in dummy.prompts[1]
