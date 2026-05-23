from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    StopReason,
    ValidationVerdict,
)
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger, HungerPolicy
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.tracing import StopReport
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.hunger_engine import HungerEngine
from hungerloop.services.model_client import DummyModelClient, ModelResponse
from hungerloop.services.sandbox_runner import SandboxRunResult
from hungerloop.services.validation_pipeline import ValidationPipelineResult
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-resume"
MISSION_ID = "mission-resume"
PHASE_ID = "phase-resume"


class _PassingSandboxRunner:
    async def run_argv(
        self,
        task_id: str,
        loop_id: int,
        argv: list[str],
        cwd: Path,
        timeout: int,
        evidence_label: str,
    ) -> SandboxRunResult:
        return SandboxRunResult(
            argv=list(argv),
            cwd=str(cwd),
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            evidence_id=f"ev-{evidence_label}-{len(argv)}",
        )


def _seed_validating_task(repo: SQLiteRepository) -> None:
    repo.create_task(TASK_ID, "Resume mission validation")
    repo.set_hunger_policy(
        TASK_ID,
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=1.0,
        ),
    )
    repo.get_hunger_clock(TASK_ID)
    item = HungerItem(
        id="H-001",
        title="write report",
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "report.md"},
            )
        ],
    )
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[item]))
    phase = MissionPhase(
        phase_id=PHASE_ID,
        title="Validation",
        description="Validate resumed state",
        feature_ids=["feature-1"],
        validation_contract_ids=["ASSERT-1"],
        status="validating",
    )
    feature = MissionFeature(
        feature_id="feature-1",
        hunger_item_id="H-001",
        phase_id=PHASE_ID,
        title="Feature",
        description="Feature under validation",
        status="done",
    )
    assertion_params = {"path": "report.md", "contains": "resumed"}
    mission = Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Resume Mission",
        description="Mission resume integration",
        phases=[phase],
        features=[feature],
        created_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
    )
    repo.save_mission(mission)
    repo.save_mission_phase(phase)
    repo.save_mission_feature(feature)
    repo.update_feature_status("feature-1", "done")
    repo.save_validation_contract(
        ValidationContract(
            mission_id=MISSION_ID,
            assertions=[
                ValidationAssertion(
                    assertion_id="ASSERT-1",
                    phase_id=PHASE_ID,
                    title="Report exists",
                    description="Report exists",
                    check_type="behavioral_assertion",
                    params=assertion_params,
                )
            ],
        )
    )
    evidence_id = repo.save_evidence(
        task_id=TASK_ID,
        loop_id=0,
        evidence_type="tool_call",
        payload={
            "success": True,
            "result_summary": "pre-sigterm report exists",
            "type": "tool_call",
        },
    )
    repo.save_best_state(
        BestState(
            task_id=TASK_ID,
            state_id="BEST-resume",
            summary="report already written",
            evidence_ids=[evidence_id],
            accepted_check_keys=["H-001:0"],
            workspace_ref="best",
        )
    )
    repo.save_accepted_check(
        task_id=TASK_ID,
        check_key="H-001:0",
        hunger_item_id="H-001",
        check_index=0,
        accepted_at_loop=0,
        validation_id="VAL-resume-seed",
        evidence_id=evidence_id,
    )
    item = item.model_copy(
        update={
            "evidence_ids": [evidence_id],
            "gap_score": 0.0,
            "status": HungerItemStatus.VALIDATED_SATISFIED,
        }
    )
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[item]))


def _seed_resume_cli_task(repo: SQLiteRepository) -> None:
    _seed_validating_task(repo)
    clock = repo.get_hunger_clock(TASK_ID)
    clock.frozen = True
    repo.save_hunger_clock(clock)
    repo.save_stop_report(
        StopReport(
            task_id=TASK_ID,
            stop_reason=StopReason.HUMAN_PAUSED,
            goal_status="paused",
        )
    )


def test_resume_preserves_phase_state_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    _seed_validating_task(repo)

    validation_result = ValidationPipelineResult(
        deterministic_report=ValidationReport(
            id="VAL-deterministic",
            task_id=TASK_ID,
            loop_id=1,
            candidate_state_id="CAND-resume",
            baseline_state_id=None,
            verdict=ValidationVerdict.PASS,
        ),
        scrutiny_report=ValidationReport(
            id="VAL-scrutiny",
            task_id=TASK_ID,
            loop_id=1,
            candidate_state_id="CAND-resume",
            baseline_state_id=None,
            verdict=ValidationVerdict.PASS,
        ),
        user_testing_report=ValidationReport(
            id="VAL-user-testing",
            task_id=TASK_ID,
            loop_id=1,
            candidate_state_id="CAND-resume",
            baseline_state_id=None,
            verdict=ValidationVerdict.PASS,
        ),
        pipeline_verdict="pass",
        stages_run=["deterministic", "scrutiny", "user_testing"],
    )

    repo.close()

    reopened = SQLiteRepository.open(db_path)
    phase_before = reopened.list_mission_phases(MISSION_ID)[0]
    assert phase_before.status == "validating"

    HungerEngine(repo=reopened).tick(
        reopened.get_hunger_policy(TASK_ID),
        reopened.get_hunger_clock(TASK_ID),
        reopened.get_hunger_ledger(TASK_ID),
        task_id=TASK_ID,
        validation_result=validation_result,
        validation_phase_id=PHASE_ID,
    )

    phase_after = reopened.list_mission_phases(MISSION_ID)[0]
    assert phase_after.status == "done"
    assert reopened.list_events(TASK_ID, event_types=["mission.phase_validated"])
    assert reopened.list_events(TASK_ID, event_types=["mission.phase_completed"])


def test_validation_only_resume_carries_best_state_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    _seed_resume_cli_task(repo)
    best_root = WorkspaceManager(tmp_path / "workspace").best_files_dir(TASK_ID)
    best_root.mkdir(parents=True)
    (best_root / "report.md").write_text("resumed\n", encoding="utf-8")

    from hungerloop.cli.orchestrator_factory import build_orchestrator

    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=tmp_path / "workspace",
        model_client=DummyModelClient(
            [
                ModelResponse(
                    content="{}",
                    json_data={"summary": "already validating", "actions": []},
                )
            ]
        ),
        max_loops_safety_cap=1,
    )
    orchestrator.validation_pipeline.scrutiny_validator.sandbox_runner = (  # type: ignore[union-attr]
        _PassingSandboxRunner()
    )

    trace = asyncio.run(orchestrator.step(TASK_ID))

    assert not isinstance(trace, StopReport)
    assert trace.committed
    assert trace.verdict == "pass"
    assert trace.validation_pipeline_trace is not None
    assert trace.validation_pipeline_trace["pipeline_verdict"] == "pass"
    phase_after = repo.list_mission_phases(MISSION_ID)[0]
    assert phase_after.status == "done"
    validation_report = repo.get_validation_report(str(trace.validation_report_id))
    assert validation_report is not None
    assert validation_report.evidence_ids
    assert not validation_report.missing_evidence


def test_mission_run_resume_after_sigterm_mid_validating(tmp_path: Path) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    _seed_resume_cli_task(repo)
    repo.close()
    sigterm_marker = tmp_path / "sigterm.marker"

    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, signal, sys, time; "
                "open(sys.argv[1], 'w', encoding='utf-8').write('validating\\n'); "
                "os.kill(os.getpid(), signal.SIGTERM); "
                "time.sleep(5)"
            ),
            str(sigterm_marker),
        ]
    )
    try:
        exit_code = worker.wait(timeout=5)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

    assert exit_code == -signal.SIGTERM
    assert sigterm_marker.read_text(encoding="utf-8") == "validating\n"

    reopened = SQLiteRepository.open(db_path)
    phase_before = reopened.list_mission_phases(MISSION_ID)[0]
    assert phase_before.status == "validating"
    best_root = WorkspaceManager(tmp_path / "workspace").best_files_dir(TASK_ID)
    best_root.mkdir(parents=True)
    (best_root / "report.md").write_text("resumed\n", encoding="utf-8")

    def _factory_with_fast_scrutiny(**kwargs: Any) -> object:
        from hungerloop.cli.orchestrator_factory import build_orchestrator

        orchestrator = build_orchestrator(**kwargs)
        orchestrator.validation_pipeline.scrutiny_validator.sandbox_runner = (  # type: ignore[union-attr]
            _PassingSandboxRunner()
        )
        return orchestrator

    run_globals = cli.commands["run"].callback.__wrapped__.__globals__
    original_factory = run_globals["build_orchestrator"]
    run_globals["build_orchestrator"] = _factory_with_fast_scrutiny
    try:
        result = CliRunner().invoke(
            cli,
            ["mission", "run", TASK_ID, "--resume", "--max-loops", "3"],
            obj=CliContext(
                repo=reopened,
                workspace_root=tmp_path / "workspace",
                model_client=DummyModelClient(
                    [
                        ModelResponse(
                            content="{}",
                            json_data={
                                "summary": "already validating",
                                "actions": [],
                            },
                        ),
                        ModelResponse(
                            content="{}",
                            json_data={
                                "summary": "phase already validated",
                                "actions": [],
                            },
                        ),
                    ]
                ),
            ),
        )
    finally:
        run_globals["build_orchestrator"] = original_factory

    assert result.exit_code == 0, result.output
    assert f"Task {TASK_ID} stopped: done" in result.output
    phase_after = reopened.list_mission_phases(MISSION_ID)[0]
    assert phase_after.status == "done"
    assert reopened.list_events(TASK_ID, event_types=["mission.phase_validated"])
    assert reopened.list_events(TASK_ID, event_types=["mission.phase_completed"])
