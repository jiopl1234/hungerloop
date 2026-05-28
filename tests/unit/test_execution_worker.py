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


def _ctx(
    workspace_ref: str = "cand",
    acceptance_criteria: list[str] | None = None,
) -> ContextPack:
    return ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="write a demo report",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref=workspace_ref,
        # Default to empty so the worker's inner self-repair loop does
        # NOT demand a successful run_shell before allowing handoff.
        # Tests that exercise the verification path explicitly pass
        # criteria (e.g. ["command: python -m pytest ..."]).
        acceptance_criteria=acceptance_criteria or [],
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


def test_execution_worker_prompt_includes_tool_arg_schemas() -> None:
    messages = ExecutionWorker._messages(_ctx())
    prompt = "\n".join(message["content"] for message in messages)

    assert "Allowed tools and args schema:" in prompt
    assert "run_shell: args = {argv: list[str] (required, non-empty)" in prompt
    assert "write_file: args = {path: str (required), content: str (required)}" in prompt
    assert '"tool_name":"write_file"' in prompt
    assert '"path":"hello.txt"' in prompt
    assert "never a command string" in prompt


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


def test_execution_worker_messages_appends_prior_replay_block() -> None:
    """`_messages(prior_replay=[...])` interleaves the replay between the
    seed user and the loop-boundary marker so the next live turn starts
    fresh after the replay text."""
    ctx = _ctx()
    replay = [
        {
            "role": "assistant",
            "content": '{"summary":"tried write A","actions":[]}',
        },
        {
            "role": "user",
            "content": "Tool results from your previous action batch:\nfoo",
        },
    ]
    messages = ExecutionWorker._messages(ctx, prior_replay=replay)
    # system, seed user, replay asst, replay user (with end marker appended)
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Replay of the last 1 action/result pair(s)" in messages[1]["content"]
    assert messages[2] == replay[0]
    assert messages[3]["role"] == "user"
    assert messages[3]["content"].startswith(replay[1]["content"])
    assert f"Begin loop {ctx.loop_id} now." in messages[3]["content"]


async def test_execution_worker_persists_inner_loop_replay_across_runs(
    tmp_path: Path,
) -> None:
    """A first run() with stitched follow-ups populates _inner_replay; a
    second run() on the same instance sees the prior pairs replayed in
    its constructed messages."""
    import json

    from hungerloop.models.usage import ModelUsage
    from hungerloop.services.model_client import ModelResponse

    # First call: emit an action that intentionally fails to trigger the
    # inner-loop stitch (run_shell against a non-existent script). The
    # follow-up call must then emit `actions: []` to handoff cleanly.
    # acceptance_criteria=[] keeps `needs_verification=False` so the empty
    # batch on iter 1 is accepted as handoff.
    fail_body: dict[str, object] = {
        "summary": "first attempt",
        "actions": [
            {
                "tool_name": "run_shell",
                "args": {"argv": ["false"], "timeout_seconds": 5},
            }
        ],
    }
    handoff_body: dict[str, object] = {"summary": "giving up", "actions": []}
    sequence = [
        ModelResponse(
            content=json.dumps(fail_body),
            json_data=fail_body,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        ),
        ModelResponse(
            content=json.dumps(handoff_body),
            json_data=handoff_body,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        ),
    ]
    client = DummyModelClient(sequence)
    worker, _, workspace = _build_worker(tmp_path, client)
    ctx_loop_1 = _ctx()
    await worker.run(context=ctx_loop_1, workspace_root=workspace)

    key = (ctx_loop_1.task_id, ctx_loop_1.agent_id)
    assert key in worker._inner_replay
    replay = worker._inner_replay[key]
    # Exactly one stitch happened (iter 0 had a failing tool, iter 1
    # handed off). new_entries = [assistant, user_followup].
    assert len(replay) == 2
    assert replay[0]["role"] == "assistant"
    assert "first attempt" in replay[0]["content"]
    assert replay[1]["role"] == "user"
    assert "Tool results from your previous action batch" in replay[1]["content"]

    # Second loop with a new ContextPack (loop_id=2). Build messages and
    # confirm the prior_replay is materially present.
    ctx_loop_2 = ContextPack(
        task_id=ctx_loop_1.task_id,
        loop_id=2,
        agent_id=ctx_loop_1.agent_id,
        mission=ctx_loop_1.mission,
        phase=ctx_loop_1.phase,
        target_hunger_item_ids=list(ctx_loop_1.target_hunger_item_ids),
        candidate_workspace_ref=ctx_loop_1.candidate_workspace_ref,
        acceptance_criteria=[],
        allowed_tools=list(ctx_loop_1.allowed_tools),
        budget=ctx_loop_1.budget,
    )
    messages_loop_2 = ExecutionWorker._messages(
        ctx_loop_2, prior_replay=worker._inner_replay[key]
    )
    seed_user = messages_loop_2[1]["content"]
    assert "Replay of the last 1 action/result pair(s)" in seed_user
    assert "Begin loop 2 now." in messages_loop_2[-1]["content"]


async def test_execution_worker_rejects_empty_handoff_when_no_writes_under_verification(
    tmp_path: Path,
) -> None:
    """With acceptance_criteria present and no write_file/patch_file in
    this loop, the worker must NOT accept an empty action batch as a
    clean handoff — even if a run_shell happened to succeed against an
    already-present artifact. This is the tightened cross-loop fix: the
    looser "any run_shell success → verified" rule let mini-sql loops
    2/3/4 hand off after only reading code, blocking progress."""
    import json

    from hungerloop.models.usage import ModelUsage
    from hungerloop.services.execution_worker import MAX_SELF_REPAIR_ITERATIONS
    from hungerloop.services.model_client import ModelResponse

    handoff_body: dict[str, object] = {"summary": "nothing to do", "actions": []}
    responses = [
        ModelResponse(
            content=json.dumps(handoff_body),
            json_data=handoff_body,
            usage=ModelUsage(input_tokens=1, output_tokens=1),
        )
        for _ in range(MAX_SELF_REPAIR_ITERATIONS + 2)
    ]

    call_count = {"n": 0}

    class _CountingClient(DummyModelClient):
        async def complete_json(self, **kw):  # type: ignore[override]
            call_count["n"] += 1
            return await super().complete_json(**kw)

    client = _CountingClient(responses)
    worker, _, workspace = _build_worker(tmp_path, client)
    ctx = _ctx(acceptance_criteria=["command: python -c 'pass'"])
    await worker.run(context=ctx, workspace_root=workspace)

    # All MAX_SELF_REPAIR_ITERATIONS+1 iterations consumed: each empty
    # handoff is pushed back ("you have NOT yet written or patched") and
    # the model (a DummyModelClient that keeps emitting empty) never
    # writes, so we exhaust the budget. Under the OLD rule this would
    # have stopped at iter 0 with 1 model call.
    assert call_count["n"] == MAX_SELF_REPAIR_ITERATIONS + 1


async def test_execution_worker_followup_surfaces_patch_file_diagnostic(
    tmp_path: Path,
) -> None:
    """A failed patch_file in iter 0 must yield a follow-up user message
    whose body includes the new closest_matches / occurrences diagnostic
    so the model has something concrete to act on in iter 1.

    Generic harness check — uses neutral text (no SQL/mission-specific
    content) so any mission benefits from the surfaced diagnostic.
    """
    import json

    from hungerloop.models.usage import ModelUsage
    from hungerloop.services.model_client import ModelResponse

    # Set up a target file with a near-miss line so closest_matches is
    # meaningful.
    (tmp_path / "alpha.txt").write_text(
        "apple\nbanana\ncherry\ndate\n", encoding="utf-8"
    )

    # iter 0: attempt to patch a line that doesn't exist (triggers
    # no_match + diagnostic).
    fail_body: dict[str, object] = {
        "summary": "first attempt",
        "actions": [
            {
                "tool_name": "patch_file",
                "args": {
                    "path": "alpha.txt",
                    "old_text": "bananna",
                    "new_text": "BANANA",
                },
            }
        ],
    }
    # iter 1: capture follow-up by handing off cleanly.
    handoff_body: dict[str, object] = {
        "summary": "stopping",
        "actions": [],
    }
    captured_messages: list[list[dict[str, str]]] = []

    class _CapturingClient(DummyModelClient):
        async def complete_json(self, **kw):  # type: ignore[override]
            captured_messages.append(list(kw["messages"]))
            return await super().complete_json(**kw)

    client = _CapturingClient(
        [
            ModelResponse(
                content=json.dumps(fail_body),
                json_data=fail_body,
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
            ModelResponse(
                content=json.dumps(handoff_body),
                json_data=handoff_body,
                usage=ModelUsage(input_tokens=1, output_tokens=1),
            ),
        ]
    )
    worker, _, workspace = _build_worker(tmp_path, client)
    # acceptance_criteria=[] so iter 1's empty actions is a clean handoff.
    await worker.run(context=_ctx(), workspace_root=workspace)

    # iter 1 should have been called with the stitched follow-up. Find
    # the trailing user message and assert the diagnostic surfaced.
    assert len(captured_messages) >= 2
    followup_messages = captured_messages[1]
    last_user = next(
        m for m in reversed(followup_messages) if m["role"] == "user"
    )
    body = last_user["content"]
    assert "Tool results from your previous action batch" in body
    assert "patch_file" in body
    # The new diagnostic header text and closest_matches block must be
    # visible — this is the regression check that previously surfaced
    # only "success=False".
    assert "old_text not found" in body
    assert "closest_matches:" in body


async def test_execution_worker_passes_retry_kwargs(tmp_path: Path) -> None:
    """Sanity-check the call site threads retry params from budget."""
    captured: dict[str, object] = {}

    class _SpyClient(DummyModelClient):
        async def complete_json(  # type: ignore[override]
            self,
            *,
            task_id: str,
            loop_id: int,
            agent_id: str,
            messages: list[dict[str, str]],
            max_tokens: int,
            max_retries: int = 0,
            retry_base_delay_seconds: float = 1.0,
            retry_max_delay_seconds: float = 20.0,
        ):
            captured["loop_id"] = loop_id
            captured["max_retries"] = max_retries
            captured["retry_base"] = retry_base_delay_seconds
            captured["retry_max"] = retry_max_delay_seconds
            return await super().complete_json(
                task_id=task_id,
                loop_id=loop_id,
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
    assert captured["loop_id"] == ctx.loop_id
