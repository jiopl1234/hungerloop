"""SQLiteRepository protocol and durability tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from hungerloop.models.blackboard import Artifact, BestState
from hungerloop.models.enums import EvidenceType, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerItem, HungerLedger, HungerPolicy, HungerSnapshot
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.planning import LoopPlan
from hungerloop.models.skill import SkillCard
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository.sqlite_repo import SQLiteRepository


def _repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository.open(tmp_path / "hungerloop.sqlite")


def test_sqlite_repository_round_trips_core_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_task("t1", "Build a report")
    repo.set_hunger_policy("t1", HungerPolicy(max_total_cost_usd=1.0))
    item = HungerItem(id="H-001", title="report")
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    clock = repo.get_hunger_clock("t1")
    clock.loop_count = 2
    repo.save_hunger_clock(clock)
    repo.save_hunger_snapshot(
        "t1",
        HungerSnapshot(
            drive_budget=50.0,
            work_pressure=10.0,
            active_hunger=10.0,
            drive_ratio=0.5,
            phase=LoopPhase.EXPLOIT,
            should_stop=False,
        ),
    )

    reopened = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    assert reopened.get_task("t1").raw_goal == "Build a report"  # type: ignore[union-attr]
    assert reopened.get_hunger_policy("t1").max_total_cost_usd == 1.0
    assert reopened.get_hunger_clock("t1").loop_count == 2
    assert reopened.get_hunger_ledger("t1").items[0].id == "H-001"
    assert reopened.get_latest_hunger_snapshot("t1").drive_budget == 50.0  # type: ignore[union-attr]


def test_sqlite_repository_evidence_usage_events_and_artifacts(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.create_task("t1", "Goal")
    model_eid = repo.save_model_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.01,
        response_preview="{}",
    )
    tool_eid = repo.save_tool_call_as_evidence(
        task_id="t1",
        loop_id=1,
        agent_id="a1",
        tool_name="write_file",
        args_summary="{}",
        result_summary="ok",
        success=True,
        elapsed_ms=1,
    )
    repo.append_event(EventType.LOOP_STARTED, {"x": 1}, task_id="t1", loop_id=1)
    repo.append_event(EventType.UNKNOWN_MODEL_PRICING, {"model": "x"})
    repo.save_artifact(Artifact(artifact_id="art-1", task_id="t1", loop_id=1, artifact_type="file"))

    assert repo.count_evidence_by_type("t1", [model_eid, tool_eid], EvidenceType.MODEL_CALL) == 1
    assert repo.count_evidence_by_type("t1", [model_eid, tool_eid], "any") == 2
    assert repo.get_usage_snapshot("t1").llm_calls == 1
    assert repo.get_usage_snapshot("t1").tool_calls == 1
    assert repo.list_events("t1")[0]["event_type"] == "loop_started"
    assert len(repo.list_events("t1")) == 1
    assert repo.get_artifacts_by_ids(["art-1"])[0].artifact_type == "file"


def test_sqlite_repository_successful_only_evidence_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_task("t1", "Goal")
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
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.01,
        response_preview="{}",
    )

    evidence_ids = [failed_tool, failed_shell, good_model]
    assert repo.count_evidence_by_type("t1", evidence_ids, "any") == 3
    assert repo.count_evidence_by_type(
        "t1", evidence_ids, "any", successful_only=True
    ) == 1
    assert repo.count_evidence_by_type(
        "t1",
        evidence_ids,
        EvidenceType.TOOL_CALL,
        successful_only=True,
    ) == 0


def test_sqlite_repository_traces_stop_memory_skill_lock_and_transaction(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.create_task("t1", "Goal")
    repo.save_best_state(BestState(task_id="t1", state_id="best-1", summary="ok"))
    repo.save_agent_spec(AgentSpec(agent_id="a1", name="A"))
    repo.save_worker_result(WorkerResult(agent_id="a1", task_id="t1", loop_id=1))
    repo.save_loop_plan(LoopPlan(task_id="t1", loop_id=1, phase=LoopPhase.EXPLORE))
    repo.save_loop_trace(
        LoopTrace(
            task_id="t1",
            loop_id=1,
            phase="explore",
            active_hunger=1.0,
            drive_budget=1.0,
            work_pressure=1.0,
            committed=True,
        )
    )
    repo.save_stop_report(
        StopReport(
            task_id="t1",
            stop_reason=StopReason.DONE,
            goal_status="completed",
        )
    )
    repo.save_memory_candidate(
        MemoryCandidate(
            candidate_id="mem-1",
            task_id="t1",
            content="fact",
            source_candidate_state_id="cand-1",
            source_validation_id="val-1",
            source_best_state_id="best-1",
        )
    )
    repo.save_skill_card(SkillCard(skill_id="skill-1", task_id="t1", name="Skill"))

    assert repo.get_agent_spec("a1").name == "A"
    assert repo.list_loop_traces("t1")[0].loop_id == 1
    assert repo.get_last_stop_reason("t1") is StopReason.DONE
    assert repo.get_last_stop_report("t1").goal_status == "completed"  # type: ignore[union-attr]
    memory = repo.list_memory_candidates("t1")[0]
    assert memory.candidate_id == "mem-1"
    assert memory.source_best_state_id == "best-1"
    assert repo.list_skill_cards("t1")[0].skill_id == "skill-1"

    assert repo.acquire_task_lock("t1", "host:1:a", stale_threshold_seconds=1800) == "acquired"
    assert repo.acquire_task_lock("t1", "host:1:b", stale_threshold_seconds=1800) == "reentrant"
    assert repo.acquire_task_lock("t1", "other:2:c", stale_threshold_seconds=1800) == "held_live"
    assert (
        repo.acquire_task_lock(
            "t1", "other:2:c", stale_threshold_seconds=1800, steal=True
        )
        == "stolen"
    )
    assert any(e["event_type"] == "lock_stolen" for e in repo.list_events("t1"))
    repo.release_task_lock("t1", "other:2:c")

    try:
        with repo.transaction():
            repo.save_skill_card(SkillCard(skill_id="rollback", task_id="t1", name="Rollback"))
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    assert all(card.skill_id != "rollback" for card in repo.list_skill_cards("t1"))


def test_sqlite_schema_rejects_bad_evidence_type(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.create_task("t1", "Goal")
    with sqlite3.connect(str(tmp_path / "hungerloop.sqlite")) as conn:
        try:
            conn.execute(
                """
                INSERT INTO evidence(evidence_id, task_id, evidence_type, payload_json)
                VALUES ('bad', 't1', 'typo', '{}')
                """
            )
        except sqlite3.IntegrityError as exc:
            assert "CHECK" in str(exc).upper()
        else:
            raise AssertionError("bad evidence_type was accepted")
