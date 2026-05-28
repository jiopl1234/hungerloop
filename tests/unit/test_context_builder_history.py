"""v0.5f ContextBuilder history and prompt golden tests."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from hungerloop.models.blackboard import BestState
from hungerloop.models.context import ContextPack
from hungerloop.models.enums import (
    AcceptanceCheckType,
    CompletionMode,
    LoopPhase,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerPolicy
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.tracing import LoopTrace
from hungerloop.models.validation import CheckResult, ValidationReport
from hungerloop.models.worker import WorkerResult
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.context_builder import (
    K_EVIDENCE_WINDOW,
    K_REJECT_WINDOW,
    MAX_HISTORY_CHARS,
    MAX_WORKSPACE_FILE_PATH_CHARS,
    MAX_WORKSPACE_FILES_LINE_CHARS,
    READ_ONLY_REJECTED_HINT,
    ContextBuilder,
    _apply_history_cap,
)
from hungerloop.services.execution_worker import ExecutionWorker
from hungerloop.services.prior_loop_context import render_prior_loop_context_block

EXPECTED_LOOP1_PROMPT = (
    """\
Mission:
[phase=explore] Make progress on H-001: Core deliverable.
User goal:
Create a file named hello.txt in the workspace root containing the word hello.

Acceptance: hello.txt exists.

Acceptance criteria:
- hello.txt exists [file_exists params={"path": "hello.txt"}]

Allowed tools and args schema:
- read_file: args = {path: str (required)}
- write_file: args = {path: str (required), content: str (required)}
- patch_file: args = {path: str (required), old_text: str (required), new_text: str (required)}
- run_shell: args = {argv: list[str] (required, non-empty), timeout: int = 60}

Required JSON shape example:
"""
    '{"summary":"created hello.txt","actions":[{"tool_name":"write_file",'
    '"args":{"path":"hello.txt","content":"hello"}}]}\n\n'
    "Use exactly the listed args shape for each tool. For run_shell, use "
    "an argv array and never a command string."
)


class StaticWorkspaceReader:
    def __init__(self, files: list[str] | None = None) -> None:
        self.files = files or []

    def list_workspace_files(
        self,
        task_id: str,
        *,
        ref: Literal["best", "candidate"],
        loop_id: int | None = None,
    ) -> list[str]:
        return sorted(self.files)


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


def _seed_item(repo: InMemoryRepository, *, path: str = "hello.txt") -> None:
    item = HungerItem(
        id="H-001",
        title="Core deliverable",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": path},
                description=f"{path} exists",
            )
        ],
    )
    repo.save_hunger_item(item)


def _build_pack(
    repo: InMemoryRepository,
    *,
    loop_id: int,
    reader: StaticWorkspaceReader | None = None,
    path: str = "hello.txt",
) -> ContextPack:
    _seed_item(repo, path=path)
    return ContextBuilder(
        repo=repo,
        workspace_reader=reader or StaticWorkspaceReader(),
    ).build_for_agent(
        task_id="t1",
        loop_id=loop_id,
        agent_id="execution_worker_v1",
        mission=(
            "[phase=explore] Make progress on H-001: Core deliverable.\n"
            "User goal:\n"
            f"Create a file named {path} in the workspace root containing the word hello.\n\n"
            f"Acceptance: {path} exists."
        ),
        target_hunger_item_ids=["H-001"],
        budget=_budget(),
        allowed_tools=["read_file", "write_file", "patch_file", "run_shell"],
        output_schema_name="default",
        candidate_workspace_ref=f"candidates/loop_{loop_id:03d}",
    )


def _failed_report(loop_id: int, *, path: str = "fizzbuzz.py") -> ValidationReport:
    return ValidationReport(
        id=f"VAL-t1-{loop_id}",
        task_id="t1",
        loop_id=loop_id,
        candidate_state_id=f"cand-{loop_id}",
        baseline_state_id=None,
        verdict=ValidationVerdict.FAIL,
        check_results=[
            CheckResult(
                hunger_item_id="H-001",
                check_index=0,
                check_key="H-001:0",
                check_type=AcceptanceCheckType.FILE_EXISTS,
                passed=False,
                detail=f"file_exists({path}): False",
            )
        ],
    )


def _seed_rejected_loop(
    repo: InMemoryRepository,
    loop_id: int,
    *,
    summary: str = "Explore directory and create fizzbuzz.py module",
    path: str = "fizzbuzz.py",
) -> str:
    report = _failed_report(loop_id, path=path)
    repo.save_validation_report(report)
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=loop_id,
            phase="explore",
            active_hunger=1.0,
            drive_budget=1.0,
            work_pressure=1.0,
            validation_report_id=report.id,
            committed=False,
        )
    )
    repo.save_worker_result(
        WorkerResult(
            agent_id="execution_worker_v1",
            task_id="t1",
            loop_id=loop_id,
            summary=summary,
        )
    )
    return repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=loop_id,
        agent_id="execution_worker_v1",
        tool_name="run_shell",
        args_summary="argv=['ls', '-la'] timeout=60",
        result_summary="exit=0 timed_out=False",
        success=True,
        elapsed_ms=1,
    )


def _seed_rejected_loop_without_tool_evidence(
    repo: InMemoryRepository,
    loop_id: int,
    *,
    summary: str = "Explore directory and create fizzbuzz.py module",
    path: str = "fizzbuzz.py",
) -> None:
    report = _failed_report(loop_id, path=path)
    repo.save_validation_report(report)
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=loop_id,
            phase="explore",
            active_hunger=1.0,
            drive_budget=1.0,
            work_pressure=1.0,
            validation_report_id=report.id,
            committed=False,
        )
    )
    repo.save_worker_result(
        WorkerResult(
            agent_id="execution_worker_v1",
            task_id="t1",
            loop_id=loop_id,
            summary=summary,
        )
    )


def _seed_committed_loop(repo: InMemoryRepository, loop_id: int) -> str:
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=loop_id,
            phase="explore",
            active_hunger=1.0,
            drive_budget=1.0,
            work_pressure=1.0,
            committed=True,
        )
    )
    repo.save_worker_result(
        WorkerResult(
            agent_id="execution_worker_v1",
            task_id="t1",
            loop_id=loop_id,
            summary="Wrote fizzbuzz module per spec",
        )
    )
    return repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=loop_id,
        agent_id="execution_worker_v1",
        tool_name="write_file",
        args_summary="path=fizzbuzz.py",
        result_summary="wrote 175 chars",
        success=True,
        elapsed_ms=1,
    )


def test_loop1_prompt_byte_identical_to_post_0568404_baseline() -> None:
    repo = InMemoryRepository()
    pack = _build_pack(repo, loop_id=1)

    assert pack.last_self_summary is None
    assert pack.failure_patterns_to_avoid == []
    assert pack.relevant_evidence_ids == []
    assert pack.relevant_evidence_summaries == []
    assert pack.best_workspace_files == []
    assert pack.truncation_info is None
    assert ExecutionWorker._messages(pack)[1]["content"] == EXPECTED_LOOP1_PROMPT


def test_loop2_after_rejected_loop1_renders_failures_and_self_summary_only() -> None:
    repo = InMemoryRepository()
    _seed_rejected_loop(repo, 1)
    repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        response_preview='{"summary":"Explore..."}',
    )
    repo.save_shell_output_as_evidence(
        "t1",
        1,
        "run_shell",
        ["ls", "-la"],
        "/tmp",
        0,
        "",
        "",
        False,
    )

    pack = _build_pack(repo, loop_id=2, path="fizzbuzz.py")

    assert pack.last_self_summary == "Explore directory and create fizzbuzz.py module"
    assert pack.failure_patterns_to_avoid == [
        "loop 1: H-001:0 file_exists → file_exists(fizzbuzz.py): False"
    ]
    assert pack.relevant_evidence_ids == []
    assert pack.relevant_evidence_summaries == []
    user_message = ExecutionWorker._messages(pack)[1]["content"]
    assert "patterns to avoid (do NOT repeat these actions" in user_message
    assert "loop 1: H-001:0 file_exists →" in user_message
    assert "last attempt summary: Explore directory and create" in user_message
    assert "successful actions already on record" not in user_message


def test_loop3_after_committed_loop2_renders_evidence_summaries() -> None:
    repo = InMemoryRepository()
    _seed_rejected_loop(repo, 1)
    evidence_id = _seed_committed_loop(repo, 2)

    pack = _build_pack(repo, loop_id=3, path="fizzbuzz.py")

    assert pack.last_self_summary == "Wrote fizzbuzz module per spec"
    assert pack.relevant_evidence_ids == [evidence_id]
    assert pack.relevant_evidence_summaries == [
        "loop 2 tool_call write_file fizzbuzz.py: wrote 175 chars"
    ]
    assert any("loop 1: H-001:0 file_exists" in line for line in pack.failure_patterns_to_avoid)
    user_message = ExecutionWorker._messages(pack)[1]["content"]
    assert "successful actions already on record" in user_message
    assert "loop 2 tool_call write_file fizzbuzz.py:" in user_message


def test_loop2_after_rejected_loop1_keeps_read_file_evidence_summary() -> None:
    repo = InMemoryRepository()
    _seed_rejected_loop(repo, 1)
    read_evidence = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        tool_name="read_file",
        args_summary="path=fizzbuzz.py",
        result_summary="def fizzbuzz(n): return str(n)",
        success=True,
        elapsed_ms=1,
    )

    pack = _build_pack(repo, loop_id=2, path="fizzbuzz.py")

    assert read_evidence in pack.relevant_evidence_ids
    assert pack.relevant_evidence_summaries == [
        "loop 1 tool_call read_file fizzbuzz.py: def fizzbuzz(n): return str(n)"
    ]
    user_message = ExecutionWorker._messages(pack)[1]["content"]
    assert "successful actions already on record" in user_message
    assert "loop 1 tool_call read_file fizzbuzz.py:" in user_message


def test_budgeted_mode_warns_after_two_read_only_rejections() -> None:
    repo = InMemoryRepository()
    repo.set_hunger_policy(
        "t1",
        HungerPolicy(completion_mode=CompletionMode.SPEND_BUDGET),
    )
    for loop_id in (1, 2):
        _seed_rejected_loop_without_tool_evidence(repo, loop_id)
        repo.save_tool_call_as_evidence(
            task_id="t1",
            loop_id=loop_id,
            agent_id="execution_worker_v1",
            tool_name="read_file",
            args_summary="path=fizzbuzz.py",
            result_summary="def fizzbuzz(n): return str(n)",
            success=True,
            elapsed_ms=1,
        )

    pack = _build_pack(repo, loop_id=3, path="fizzbuzz.py")

    assert pack.failure_patterns_to_avoid[0] == READ_ONLY_REJECTED_HINT
    user_message = ExecutionWorker._messages(pack)[1]["content"]
    assert READ_ONLY_REJECTED_HINT in user_message


def test_default_mode_does_not_warn_after_two_read_only_rejections() -> None:
    repo = InMemoryRepository()
    for loop_id in (1, 2):
        _seed_rejected_loop_without_tool_evidence(repo, loop_id)
        repo.save_tool_call_as_evidence(
            task_id="t1",
            loop_id=loop_id,
            agent_id="execution_worker_v1",
            tool_name="read_file",
            args_summary="path=fizzbuzz.py",
            result_summary="def fizzbuzz(n): return str(n)",
            success=True,
            elapsed_ms=1,
        )

    pack = _build_pack(repo, loop_id=3, path="fizzbuzz.py")

    assert READ_ONLY_REJECTED_HINT not in pack.failure_patterns_to_avoid


def test_static_workspace_reader_inventory_sorts_and_truncates() -> None:
    repo = InMemoryRepository()
    reader = StaticWorkspaceReader(
        ["fizzbuzz.py", "test_fizzbuzz.py", "__pycache__/x.pyc", ".pytest_cache/v"]
    )
    pack = _build_pack(repo, loop_id=1, reader=reader)

    assert pack.best_workspace_files == [
        ".pytest_cache/v",
        "__pycache__/x.pyc",
        "fizzbuzz.py",
        "test_fizzbuzz.py",
    ]


def test_workspace_manager_filters_best_inventory(tmp_path: Path) -> None:
    from hungerloop.services.workspace_manager import WorkspaceManager

    manager = WorkspaceManager(tmp_path)
    root = manager.best_files_dir("t1")
    (root / "fizzbuzz.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "fizzbuzz.py").write_text("x", encoding="utf-8")
    (root / "test_fizzbuzz.py").write_text("x", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    (root / ".pytest_cache" / "v").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "nodeids").write_text("x", encoding="utf-8")

    pack = _build_pack(repo=InMemoryRepository(), loop_id=1, reader=manager)

    assert pack.best_workspace_files == ["fizzbuzz.py", "test_fizzbuzz.py"]


def test_20_file_and_long_path_truncation() -> None:
    repo = InMemoryRepository()
    long_name = "a" + ("x" * (MAX_WORKSPACE_FILE_PATH_CHARS + 20)) + ".txt"
    reader = StaticWorkspaceReader([long_name] + [f"f{i:02d}.txt" for i in range(1, 26)])

    pack = _build_pack(repo, loop_id=1, reader=reader)

    assert len(pack.best_workspace_files) == 21
    rendered = "files in best/: " + ", ".join(pack.best_workspace_files)
    assert len(rendered) <= MAX_WORKSPACE_FILES_LINE_CHARS
    assert any(entry.endswith("…") for entry in pack.best_workspace_files if "x" in entry)


def test_reject_window_caps_history() -> None:
    # Seed enough rejected loops to exceed K_REJECT_WINDOW so we can verify
    # the cap. With K_REJECT_WINDOW=5 we expect the 5 most recent rejected
    # loops in the failure_patterns and earlier ones excluded.
    repo = InMemoryRepository()
    n_seeded = K_REJECT_WINDOW + 2  # 2 should fall outside the window
    for loop_id in range(1, n_seeded + 1):
        _seed_rejected_loop(repo, loop_id, summary=f"summary {loop_id}")

    pack = _build_pack(repo, loop_id=n_seeded + 1, path="fizzbuzz.py")

    joined = "\n".join(pack.failure_patterns_to_avoid)
    # Top K_REJECT_WINDOW most-recent loops should be present.
    for kept_loop in range(n_seeded, n_seeded - K_REJECT_WINDOW, -1):
        assert f"loop {kept_loop}:" in joined
    # Loops older than the window should be dropped.
    for dropped_loop in range(1, n_seeded - K_REJECT_WINDOW + 1):
        assert f"loop {dropped_loop}:" not in joined


def test_evidence_window_excludes_loops_before_k_window() -> None:
    repo = InMemoryRepository()
    old_evidence = _seed_committed_loop(repo, 1)
    boundary_evidence = _seed_committed_loop(repo, 2)
    recent_evidence = _seed_committed_loop(repo, 3)

    pack = _build_pack(repo, loop_id=4, path="fizzbuzz.py")

    assert K_EVIDENCE_WINDOW == 2
    assert recent_evidence in pack.relevant_evidence_ids
    assert boundary_evidence in pack.relevant_evidence_ids
    assert old_evidence not in pack.relevant_evidence_ids
    joined = "\n".join(pack.relevant_evidence_summaries)
    assert "loop 3 tool_call write_file" in joined
    assert "loop 2 tool_call write_file" in joined
    assert "loop 1 tool_call write_file" not in joined


def test_history_truncation_info_and_determinism() -> None:
    repo = InMemoryRepository()
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="best-1",
            summary="best " + ("b" * 1200),
        )
    )
    for loop_id in range(1, 6):
        report = ValidationReport(
            id=f"VAL-t1-{loop_id}",
            task_id="t1",
            loop_id=loop_id,
            candidate_state_id=f"cand-{loop_id}",
            baseline_state_id=None,
            verdict=ValidationVerdict.FAIL,
            check_results=[
                CheckResult(
                    hunger_item_id="H-001",
                    check_index=0,
                    check_key="H-001:0",
                    check_type=AcceptanceCheckType.FILE_EXISTS,
                    passed=False,
                    detail=f"file_exists(fizzbuzz_{loop_id}.py): False " + ("f" * 180),
                )
                for _ in range(4)
            ],
        )
        repo.save_validation_report(report)
        repo.save_loop_trace(
            LoopTrace(
                task_id="t1",
                loop_id=loop_id,
                phase="explore",
                active_hunger=1.0,
                drive_budget=1.0,
                work_pressure=1.0,
                validation_report_id=report.id,
                committed=False,
            )
        )
        repo.save_worker_result(
            WorkerResult(
                agent_id="execution_worker_v1",
                task_id="t1",
                loop_id=loop_id,
                summary=f"summary {loop_id}",
            )
        )
    for loop_id in range(6, 8):
        _seed_committed_loop(repo, loop_id)
        for index in range(6):
            repo.save_tool_call_as_evidence(
                task_id="t1",
                loop_id=loop_id,
                agent_id="execution_worker_v1",
                tool_name="write_file",
                args_summary=f"path={index}",
                result_summary="x" * 180,
                success=True,
                elapsed_ms=1,
            )
    repo.save_worker_result(
        WorkerResult(
            agent_id="execution_worker_v1",
            task_id="t1",
            loop_id=7,
            summary="last",
        )
    )

    reader = StaticWorkspaceReader([f"{'p' * 80}_{index}.txt" for index in range(20)])
    pack1 = _build_pack(repo, loop_id=8, reader=reader, path="fizzbuzz.py")
    pack2 = _build_pack(repo, loop_id=8, reader=reader, path="fizzbuzz.py")

    assert pack1.truncation_info is not None
    assert pack1.truncation_info.chars_before > MAX_HISTORY_CHARS
    assert pack1.truncation_info.chars_after <= MAX_HISTORY_CHARS
    rendered_history = render_prior_loop_context_block(
        loop_id=pack1.loop_id,
        last_self_summary=pack1.last_self_summary,
        prior_handoff_summary=pack1.prior_handoff_summary,
        best_state_summary=pack1.best_state_summary,
        best_workspace_files=pack1.best_workspace_files,
        failure_patterns_to_avoid=pack1.failure_patterns_to_avoid,
        relevant_evidence_summaries=pack1.relevant_evidence_summaries,
    )
    assert len(rendered_history) == pack1.truncation_info.chars_after
    assert (
        pack1.truncation_info.dropped_evidence
        + pack1.truncation_info.dropped_failures
        > 0
    )
    assert pack1.model_dump_json() == pack2.model_dump_json()


def test_best_summary_clip_does_not_emit_total_truncation_info() -> None:
    repo = InMemoryRepository()
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="best-1",
            summary="best " + ("b" * 1200),
        )
    )

    pack = _build_pack(repo, loop_id=1)

    assert pack.best_state_summary is not None
    assert pack.best_state_summary.endswith("…")
    assert pack.truncation_info is None


def test_real_llm_loop_memory_smoke_is_gated() -> None:
    path = Path("tests/integration/test_real_llm_loop_memory.py")
    assert path.exists()


def test_apply_history_cap_does_not_assert_when_static_block_exceeds_cap() -> None:
    """Static-block-overflow degrades gracefully (post-PR #1 fixup #1).

    Prior to this fix, ``_apply_history_cap`` raised ``AssertionError``
    when the non-evictable static block (last_summary + best_summary +
    best_files + headers) alone exceeded ``MAX_HISTORY_CHARS``. The
    eviction loops cannot help in that case (there's nothing left to
    drop), and the bare ``assert`` would propagate up to
    ``LoopOrchestrator._step_inner`` and surface as
    ``stop_reason=ERROR``.

    The function now returns the over-cap state via TruncationInfo so
    downstream consumers can audit it, and the loop continues.
    """
    # Construct synthetic inputs whose non-evictable static block alone
    # is already larger than MAX_HISTORY_CHARS so eviction cannot help.
    huge_last_summary = "L" * (MAX_HISTORY_CHARS // 2)
    huge_best_summary = "B" * (MAX_HISTORY_CHARS // 2)
    huge_best_files = ["F" * 100 for _ in range(8)]

    (
        _prior_handoff_summary,
        _last_summary,
        evidence_ids,
        evidence_lines,
        failure_lines,
        info,
    ) = _apply_history_cap(
        loop_id=42,
        prior_handoff_summary="",
        last_summary=huge_last_summary,
        best_summary=huge_best_summary,
        best_files=huge_best_files,
        evidence_ids=[],
        evidence_lines=[],
        failure_lines=[],
        best_summary_truncated=False,
    )

    assert info is not None
    # chars_after MUST be allowed to exceed MAX_HISTORY_CHARS — the
    # whole point of this test is that the function does NOT raise.
    assert info.chars_after > MAX_HISTORY_CHARS
    # Eviction lists are unchanged (nothing to evict).
    assert evidence_ids == []
    assert evidence_lines == []
    assert failure_lines == []
    assert info.dropped_evidence == 0
    assert info.dropped_failures == 0
