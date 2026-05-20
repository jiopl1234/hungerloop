from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import AcceptanceCheckType, LoopPhase, ValidationVerdict
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.commit_manager import CommitManager
from hungerloop.services.cost_guard import CostGuard
from hungerloop.services.handoff_processor import HandoffProcessor
from hungerloop.services.sandbox_runner import SandboxRunResult
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validation_pipeline import ValidationPipeline
from hungerloop.services.validators.scrutiny_validator import ScrutinyValidator
from hungerloop.services.validators.user_testing_validator import UserTestingValidator
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
MISSION_ID = "mission-1"
PHASE_ID = "phase-1"
LOOP_ID = 1


class _PassingSandboxRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_argv(
        self,
        task_id: str,
        loop_id: int,
        argv: list[str],
        cwd: Path,
        timeout: int,
        evidence_label: str,
    ) -> SandboxRunResult:
        self.calls.append(
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "argv": list(argv),
                "cwd": cwd,
                "timeout": timeout,
                "evidence_label": evidence_label,
            }
        )
        evidence_id = f"ev-{len(self.calls)}"
        return SandboxRunResult(
            argv=list(argv),
            cwd=str(cwd),
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            evidence_id=evidence_id,
        )


def _seed_mission_repo(tmp_path: Path) -> tuple[SQLiteRepository, WorkspaceManager]:
    repo = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    repo.create_task(TASK_ID, "mission validation pipeline")
    repo.set_hunger_policy(
        TASK_ID,
        HungerPolicy(
            max_total_cost_usd=10.0,
            max_total_tokens=1_000_000,
            initial_hunger=100.0,
            decay_duration_seconds=10.0,
        ),
    )
    repo.get_hunger_clock(TASK_ID)
    item = HungerItem(
        id="H-001",
        title="write report",
        priority=1.0,
        gap_score=1.0,
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
        title="Phase",
        description="Validation phase",
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
    mission = Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Mission",
        description="Mission description",
        phases=[phase],
        features=[feature],
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    repo.save_mission(mission)
    repo.save_mission_phase(phase)
    repo.save_mission_feature(feature)
    repo.save_validation_contract(
        ValidationContract(
            mission_id=MISSION_ID,
            assertions=[
                ValidationAssertion(
                    assertion_id="ASSERT-1",
                    phase_id=PHASE_ID,
                    title="Contract assertion",
                    description="Contract assertion",
                    check_type="behavioral_assertion",
                    params={"file": "report.md", "contains": ["ok"]},
                )
            ],
        )
    )
    workspace_manager = WorkspaceManager(tmp_path / "workspace")
    candidate_files = workspace_manager.create_candidate_workspace(TASK_ID, LOOP_ID)
    (candidate_files / "report.md").write_text("ok", encoding="utf-8")
    return repo, workspace_manager


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-1",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=["candidate-ev"],
    )


async def test_mission_validation_pipeline_scrutiny_result_keeps_commit_deterministic(
    tmp_path: Path,
) -> None:
    repo, workspace_manager = _seed_mission_repo(tmp_path)
    sandbox = _PassingSandboxRunner()
    validation_gate = ValidationGate(
        repo,
        AcceptanceCheckRunner(repo, workspace_manager, sandbox),  # type: ignore[arg-type]
    )
    handoff_processor = HandoffProcessor(repo)
    pipeline = ValidationPipeline.from_validation_gate(
        repo=repo,
        cost_guard=CostGuard(repo),
        validation_gate=validation_gate,
        scrutiny_validator=ScrutinyValidator(
            repo=repo,
            sandbox_runner=sandbox,  # type: ignore[arg-type]
            workspace_manager=workspace_manager,
            handoff_processor=handoff_processor,
        ),
        user_testing_validator=UserTestingValidator(
            repo=repo,
            sandbox_runner=sandbox,  # type: ignore[arg-type]
            workspace_manager=workspace_manager,
        ),
    )
    mission = repo.get_mission(TASK_ID)
    assert mission is not None
    phase = repo.list_mission_phases(MISSION_ID)[0]

    result = await pipeline.run(
        TASK_ID,
        LOOP_ID,
        _candidate(),
        ["H-001"],
        mission=mission,
        phase=phase,
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert result.stages_run == ["deterministic", "scrutiny", "user_testing"]
    assert result.deterministic_report.verdict is ValidationVerdict.PASS
    assert result.deterministic_report.newly_passed_check_keys == ["H-001:0"]
    assert result.scrutiny_report is not None
    assert result.scrutiny_report.verdict is ValidationVerdict.PASS
    assert result.user_testing_report is not None
    assert result.user_testing_report.verdict is ValidationVerdict.PASS

    decision = CommitManager(repo, workspace_manager).apply(_candidate(), result)
    best_state = repo.get_best_state(TASK_ID)

    assert decision["committed"] is True
    assert best_state is not None
    assert best_state.validation_id == result.deterministic_report.id
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_started"])
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_completed"])
