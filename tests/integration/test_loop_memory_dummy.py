"""Synthetic cross-loop memory regression."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import AcceptanceCheckType, StopReason
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.tracing import StopReport
from hungerloop.models.usage import ModelUsage
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.repository.sqlite_repo import SQLiteRepository
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


def _seed(repo: RepositoryProtocol, *, draft_sampling_k: int = 1) -> None:
    # create_task is a no-op-ish upsert on InMemoryRepository but required
    # by SQLiteRepository (FK from hunger_policy/hunger_ledger -> tasks),
    # so seeding it unconditionally keeps this helper backend-agnostic.
    repo.create_task("t1", "hello smoke")
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=100_000,
            draft_sampling_k=draft_sampling_k,
        ),
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
                        {
                            "tool_name": "run_shell",
                            "args": {"argv": [sys.executable, "-c", "pass"]},
                        },
                ],
            ),
            _response(
                "write hello",
                [
                    {
                        "tool_name": "write_file",
                        "args": {"path": "hello.txt", "content": "hi"},
                    },
                        {
                            "tool_name": "run_shell",
                            "args": {"argv": [sys.executable, "-c", "pass"]},
                        },
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
    cross_loop_prompts = [
        prompt for prompt in dummy.prompts if "Prior loop context" in prompt
    ]
    assert cross_loop_prompts, "loop 2 must receive prior-loop context"
    loop_two_prompt = cross_loop_prompts[0]
    assert "patterns to avoid" in loop_two_prompt
    assert "H-001:0 file_exists" in loop_two_prompt
    assert "last attempt summary: explore" in loop_two_prompt


def _write_hello_actions() -> list[dict[str, object]]:
    return [
        {"tool_name": "write_file", "args": {"path": "hello.txt", "content": "hi"}},
        {
            "tool_name": "run_shell",
            "args": {"argv": [sys.executable, "-c", "pass"]},
        },
    ]


async def _run_hello_smoke(
    repo: RepositoryProtocol,
    workspace_root: Path,
    *,
    draft_sampling_k: int,
) -> StopReport:
    """Drive the single-item ``hello.txt`` scenario to completion.

    v0.7.2 nuance: when draft sampling is on, draft 2 DOES run a full
    worker pass before ``LoopOrchestrator._run_draft_sampling``'s
    byte-hash comparison discards it as a duplicate -- that comparison is
    how the short-circuit detects "this draft reproduced the last one".
    ``DummyModelClient.complete_json`` pops one scripted response per call
    and, once its script list is exhausted, falls back to an empty
    ``actions=[]`` response rather than replaying the last entry (see
    ``DummyModelClient`` docstring in ``services/model_client.py``). A
    script with only ONE "write hello" entry would therefore make draft 2
    diverge from draft 1 (no write vs. a write) and the short-circuit
    would never fire. Seeding the identical response twice lets draft 2
    reproduce draft 1 byte-for-byte, exactly like two consecutive calls to
    a temperature-0 real provider would.
    """
    _seed(repo, draft_sampling_k=draft_sampling_k)
    dummy = DummyModelClient(
        [_response("write hello", _write_hello_actions()) for _ in range(2)]
    )
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace_root,
        model_client=dummy,
        max_loops_safety_cap=3,
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")
    return await orchestrator.run("t1")


def _assert_draft_sampling_matches_k1(
    *,
    task_id: str,
    k1_repo: RepositoryProtocol,
    k1_report: StopReport,
    k3_repo: RepositoryProtocol,
    k3_report: StopReport,
) -> None:
    events = [e for e in k3_repo.list_events(task_id) if e["event_type"] == "draft_sampled"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    # dummy provider is deterministic -> draft 2 reproduces draft 1 and
    # the short-circuit stops sampling after the duplicate is detected.
    assert payload["draft_count"] == 1
    assert payload["worker_passes_run"] == 2
    assert payload["short_circuited_draft_indexes"] == [2]
    assert payload["short_circuited_count"] == 1
    assert payload["winner_draft_index"] == 1

    # k=1 (the default) never enters the draft-sampling branch at all.
    assert [
        e for e in k1_repo.list_events(task_id) if e["event_type"] == "draft_sampled"
    ] == []

    # Final outcome (commit + best state) must be identical to k=1.
    assert k3_report.stop_reason is StopReason.DONE
    assert k1_report.stop_reason is StopReason.DONE

    k1_best = k1_repo.get_best_state(task_id)
    k3_best = k3_repo.get_best_state(task_id)
    assert k1_best is not None
    assert k3_best is not None
    assert k3_best.state_id == k1_best.state_id
    assert k3_best.accepted_check_keys == k1_best.accepted_check_keys


async def test_loop_memory_dummy_draft_sampling_short_circuits_and_matches_k1(
    tmp_path: Path,
) -> None:
    task_id = "t1"
    k1_repo = InMemoryRepository()
    k1_report = await _run_hello_smoke(k1_repo, tmp_path / "k1", draft_sampling_k=1)
    k3_repo = InMemoryRepository()
    k3_report = await _run_hello_smoke(k3_repo, tmp_path / "k3", draft_sampling_k=3)

    _assert_draft_sampling_matches_k1(
        task_id=task_id,
        k1_repo=k1_repo,
        k1_report=k1_report,
        k3_repo=k3_repo,
        k3_report=k3_report,
    )


async def test_loop_memory_dummy_draft_sampling_short_circuits_and_matches_k1_sqlite(
    tmp_path: Path,
) -> None:
    """Same smoke as the InMemory variant, but on SQLiteRepository.

    The draft-sampling handoff-collision fix (``delete_worker_handoffs``
    before each re-run and before restoring the winner in
    ``LoopOrchestrator._run_draft_sampling``) guards against a PRIMARY KEY
    ``IntegrityError`` on ``worker_handoffs.handoff_id`` (see
    ``v6__mission_runtime.sql``): re-running draft 2 under the same loop_id
    re-derives the same deterministic handoff id as draft 1
    (``WH-<task>-<loop>-<assignment_id>``). InMemoryRepository has no such
    constraint and silently overwrites, so only a SQLite-backed run
    exercises this guard.
    """
    task_id = "t1"
    k1_repo = SQLiteRepository(tmp_path / "k1.sqlite")
    k3_repo = SQLiteRepository(tmp_path / "k3.sqlite")
    try:
        k1_report = await _run_hello_smoke(
            k1_repo, tmp_path / "k1", draft_sampling_k=1
        )
        k3_report = await _run_hello_smoke(
            k3_repo, tmp_path / "k3", draft_sampling_k=3
        )

        _assert_draft_sampling_matches_k1(
            task_id=task_id,
            k1_repo=k1_repo,
            k1_report=k1_report,
            k3_repo=k3_repo,
            k3_report=k3_report,
        )
    finally:
        # Close WAL connections deterministically — leaked handles hold
        # file locks and flake tmp_path teardown on Windows.
        k1_repo.close()
        k3_repo.close()
