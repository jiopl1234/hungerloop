"""Unit tests for InMemoryRepository new protocol methods (v0.5a)."""
from __future__ import annotations

from hungerloop.models.blackboard import Artifact
from hungerloop.models.enums import EvidenceType, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.planning import LoopPlan
from hungerloop.models.skill import SkillCard
from hungerloop.models.tracing import StopReport
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository.in_memory_repo import InMemoryRepository


def test_save_hunger_ledger_persists_items() -> None:
    repo = InMemoryRepository()
    ledger = HungerLedger(task_id="t1", items=[])
    repo.save_hunger_ledger("t1", ledger)
    assert repo.get_hunger_ledger("t1").task_id == "t1"


def test_save_accepted_check_round_trips() -> None:
    repo = InMemoryRepository()
    repo.save_accepted_check(
        task_id="t1",
        check_key="H-001:0",
        hunger_item_id="H-001",
        check_index=0,
        accepted_at_loop=1,
        validation_id="VAL-t1-1",
        evidence_id="ev-1",
    )
    # InMemory stores in _accepted_checks dict; no getter in protocol yet,
    # but we can verify it doesn't raise.
    assert ("t1", "H-001:0") in repo._accepted_checks


def test_save_model_call_as_evidence_returns_id() -> None:
    repo = InMemoryRepository()
    eid = repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        response_preview="ok",
    )
    assert eid.startswith("ev-")
    assert repo._evidence[eid]["type"] == EvidenceType.MODEL_CALL.value


def test_save_model_error_as_evidence_nullable_loop_id() -> None:
    repo = InMemoryRepository()
    eid = repo.save_model_error_as_evidence(
        task_id="t1",
        loop_id=None,
        agent_id="a1",
        provider="openai",
        model="gpt-4o",
        error_type="auth_error",
        error_message="401",
        retryable=False,
    )
    assert repo._evidence[eid]["loop_id"] is None


def test_save_tool_call_as_evidence_increments_usage() -> None:
    repo = InMemoryRepository()
    eid = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="read_file",
        args_summary="path=foo.py",
        result_summary="ok",
        success=True,
        elapsed_ms=10,
    )
    usage = repo.get_usage_snapshot("t1")
    assert usage.tool_calls == 1
    assert eid.startswith("ev-")


def test_count_evidence_by_type_enum() -> None:
    repo = InMemoryRepository()
    e1 = repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        response_preview="",
    )
    e2 = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="read_file",
        args_summary="",
        result_summary="",
        success=True,
        elapsed_ms=1,
    )
    assert repo.count_evidence_by_type("t1", [e1, e2], EvidenceType.MODEL_CALL) == 1
    assert repo.count_evidence_by_type("t1", [e1, e2], "any") == 2


def test_count_evidence_by_type_successful_only_filters_failures() -> None:
    repo = InMemoryRepository()
    failed_tool = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="run_shell",
        args_summary="argv=<missing>",
        result_summary="bad_args",
        success=False,
        elapsed_ms=1,
    )
    failed_shell = repo.save_shell_output_as_evidence(
        task_id="t1",
        loop_id=1,
        label="run_shell",
        argv=["false"],
        cwd="/tmp",
        exit_code=1,
        stdout="",
        stderr="",
        timed_out=False,
    )
    good_model = repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        response_preview="",
    )

    assert repo.count_evidence_by_type(
        "t1", [failed_tool, failed_shell, good_model], "any"
    ) == 3
    assert repo.count_evidence_by_type(
        "t1",
        [failed_tool, failed_shell, good_model],
        "any",
        successful_only=True,
    ) == 1


def test_agent_spec_round_trip() -> None:
    repo = InMemoryRepository()
    spec = AgentSpec(agent_id="a1", name="Worker", kind="execution")
    repo.save_agent_spec(spec)
    assert repo.get_agent_spec("a1").name == "Worker"


def test_worker_result_persists() -> None:
    repo = InMemoryRepository()
    result = WorkerResult(agent_id="a1", task_id="t1", loop_id=1, summary="done")
    repo.save_worker_result(result)
    # No getter in protocol; verify no raise.


def test_loop_plan_persists() -> None:
    repo = InMemoryRepository()
    plan = LoopPlan(task_id="t1", loop_id=1, phase=LoopPhase.EXPLORE)
    repo.save_loop_plan(plan)
    assert repo._loop_plans[("t1", 1)].phase == LoopPhase.EXPLORE


def test_stop_report_history() -> None:
    repo = InMemoryRepository()
    repo.save_stop_report(
        StopReport(
            task_id="t1", stop_reason=StopReason.DONE, goal_status="completed"
        )
    )
    repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.HUNGER_EXPIRED,
            goal_status="abandoned",
        )
    )
    assert repo.get_last_stop_reason("t1") == StopReason.HUNGER_EXPIRED


def test_get_usage_snapshot_default() -> None:
    repo = InMemoryRepository()
    usage = repo.get_usage_snapshot("t1")
    assert usage.task_id == "t1"
    assert usage.tokens == 0


def test_append_event_with_task_and_loop() -> None:
    repo = InMemoryRepository()
    repo.append_event(
        EventType.LOOP_STARTED, {"key": "val"}, task_id="t1", loop_id=1
    )
    assert len(repo._events) == 1
    assert repo._events[0]["task_id"] == "t1"
    assert repo._events[0]["loop_id"] == 1
    # Stored representation is the string value so SQL columns and JSON
    # serialization stay clean (PRD §22.8).
    assert repo._events[0]["event_type"] == "loop_started"


def test_memory_candidate_round_trip() -> None:
    repo = InMemoryRepository()
    mc = MemoryCandidate(candidate_id="mc-1", task_id="t1", content="fact")
    repo.save_memory_candidate(mc)
    assert repo.list_memory_candidates("t1")[0].candidate_id == "mc-1"


def test_skill_card_round_trip() -> None:
    repo = InMemoryRepository()
    card = SkillCard(skill_id="sk-1", task_id="t1", name="Fix")
    repo.save_skill_card(card)
    assert repo.list_skill_cards("t1")[0].name == "Fix"
    assert repo.list_skill_cards(None)[0].skill_id == "sk-1"


def test_transaction_context_manager() -> None:
    repo = InMemoryRepository()
    with repo.transaction():
        pass  # no-op for in-memory


def test_save_artifact_helper() -> None:
    """save_artifact is a test helper (not in protocol)."""
    repo = InMemoryRepository()
    art = Artifact(artifact_id="a1", task_id="t1", loop_id=1, artifact_type="patch")
    repo.save_artifact(art)
    assert repo.get_artifacts_by_ids(["a1"])[0].artifact_type == "patch"
