from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import EvidenceType, LoopPhase, ValidationVerdict
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.mission import Mission, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation_contract import ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.sandbox_runner import SandboxRunResult
from hungerloop.services.validators.scrutiny_validator import ScrutinyValidator
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
LOOP_ID = 4
MISSION_ID = "mission-1"
PHASE_ID = "phase-1"

PYTEST_ARGV = ["python", "-m", "pytest", "-q"]
RUFF_ARGV = ["ruff", "check", "src", "tests"]
MYPY_ARGV = ["mypy", "--strict", "src/"]
SCRUTINY_ARGVS = [PYTEST_ARGV, RUFF_ARGV, MYPY_ARGV]


class _RecordingSandboxRunner:
    def __init__(self, results: list[SandboxRunResult]) -> None:
        self.results = list(results)
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
        result = self.results.pop(0)
        return result.model_copy(update={"argv": list(argv), "cwd": str(cwd)})


def _sandbox_result(
    *,
    exit_code: int = 0,
    evidence_id: str,
    timed_out: bool = False,
    stdout: str = "",
    stderr: str = "",
) -> SandboxRunResult:
    return SandboxRunResult(
        argv=[],
        cwd="",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        evidence_id=evidence_id,
    )


def _success_results() -> list[SandboxRunResult]:
    return [
        _sandbox_result(evidence_id="ev-pytest"),
        _sandbox_result(evidence_id="ev-ruff"),
        _sandbox_result(evidence_id="ev-mypy"),
    ]


def _candidate() -> CandidateState:
    return CandidateState(
        id="CAND-task-1-4",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=["candidate-ev"],
    )


def _phase() -> MissionPhase:
    return MissionPhase(
        phase_id=PHASE_ID,
        title="Phase",
        description="Phase description",
        status="validating",
    )


def _mission(phase: MissionPhase) -> Mission:
    return Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Mission",
        description="Mission description",
        phases=[phase],
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )


def _budget(
    *,
    max_new_items_per_loop: int = 3,
    scrutiny_timeout_seconds: int = 7,
) -> BudgetAllocation:
    return BudgetAllocation(
        phase=LoopPhase.EXPLORE,
        max_new_items_per_loop=max_new_items_per_loop,
        scrutiny_timeout_seconds=scrutiny_timeout_seconds,
    )


def _setup(
    tmp_path: Path,
    results: list[SandboxRunResult],
    *,
    create_candidate_files: bool = True,
) -> tuple[
    ScrutinyValidator,
    InMemoryRepository,
    _RecordingSandboxRunner,
    CandidateState,
    ValidationContract,
    MissionPhase,
    Path,
]:
    repo = InMemoryRepository()
    repo.create_task(TASK_ID, "Run scrutiny")
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[]))
    phase = _phase()
    repo.save_mission(_mission(phase))
    contract = ValidationContract(mission_id=MISSION_ID)
    repo.save_validation_contract(contract)

    workspace_manager = WorkspaceManager(tmp_path)
    candidate_files = workspace_manager.candidate_files_dir(TASK_ID, LOOP_ID)
    if create_candidate_files:
        candidate_files.mkdir(parents=True, exist_ok=True)

    sandbox = _RecordingSandboxRunner(results)
    validator = ScrutinyValidator(
        repo=repo,
        sandbox_runner=sandbox,
        workspace_manager=workspace_manager,
    )
    return validator, repo, sandbox, _candidate(), contract, phase, candidate_files


async def test_pytest_invoked_via_sandbox(tmp_path: Path) -> None:
    validator, _repo, sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        _success_results(),
    )

    await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert sandbox.calls[0]["argv"] == PYTEST_ARGV
    assert sandbox.calls[0]["cwd"] == candidate_files.resolve()


async def test_missing_candidate_workspace_blocks_without_creating_directory(
    tmp_path: Path,
) -> None:
    validator, repo, sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        _success_results(),
        create_candidate_files=False,
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertions = repo.list_validation_assertions(
        mission_id=MISSION_ID,
        phase_id=PHASE_ID,
    )
    missing_workspace_events = repo.list_events(
        TASK_ID,
        event_types=["validation.scrutiny_workspace_missing"],
    )

    assert candidate_files.exists() is False
    assert sandbox.calls == []
    assert report.verdict is ValidationVerdict.FAIL
    assert len(assertions) == 1
    assert assertions[0].check_type == "scrutiny_workspace"
    assert assertions[0].status == "blocked"
    assert assertions[0].evidence_ids == report.evidence_ids
    assert len(report.evidence_ids) == 1
    evidence = repo._evidence[report.evidence_ids[0]]
    assert evidence["type"] == EvidenceType.VALIDATION_CHECK.value
    assert evidence["reason"] == "candidate_workspace_missing"
    assert evidence["candidate_state_id"] == candidate.id
    assert len(missing_workspace_events) == 1
    assert missing_workspace_events[0]["payload"]["reason"] == (
        "candidate_workspace_missing"
    )
    assert missing_workspace_events[0]["payload"]["phase_id"] == PHASE_ID


async def test_ruff_invoked_via_sandbox(tmp_path: Path) -> None:
    validator, _repo, sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        _success_results(),
    )

    await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert sandbox.calls[1]["argv"] == RUFF_ARGV


async def test_mypy_invoked_via_sandbox(tmp_path: Path) -> None:
    validator, _repo, sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        _success_results(),
    )

    await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert sandbox.calls[2]["argv"] == MYPY_ARGV


async def test_exit_zero_passed_and_timeout_override(tmp_path: Path) -> None:
    validator, repo, sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        _success_results(),
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(scrutiny_timeout_seconds=7),
    )

    assertions_by_type = {
        assertion.check_type: assertion
        for assertion in repo.list_validation_assertions(
            mission_id=MISSION_ID,
            phase_id=PHASE_ID,
        )
    }
    assert report.verdict is ValidationVerdict.PASS
    assert set(assertions_by_type) == {
        "scrutiny_test",
        "scrutiny_lint",
        "scrutiny_typecheck",
    }
    assert all(assertion.status == "passed" for assertion in assertions_by_type.values())
    assert assertions_by_type["scrutiny_test"].evidence_ids == ["ev-pytest"]
    assert assertions_by_type["scrutiny_lint"].evidence_ids == ["ev-ruff"]
    assert assertions_by_type["scrutiny_typecheck"].evidence_ids == ["ev-mypy"]
    assert report.evidence_ids == ["ev-pytest", "ev-ruff", "ev-mypy"]
    assert [call["timeout"] for call in sandbox.calls] == [7, 7, 7]


async def test_failed_test_injects_followup(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [
            _sandbox_result(
                exit_code=1,
                evidence_id="ev-pytest",
                stderr="pytest failed one assertion",
            ),
            _sandbox_result(evidence_id="ev-ruff"),
            _sandbox_result(evidence_id="ev-mypy"),
        ],
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertions = repo.list_validation_assertions(
        mission_id=MISSION_ID,
        phase_id=PHASE_ID,
    )
    pytest_assertion = next(
        assertion for assertion in assertions if assertion.check_type == "scrutiny_test"
    )
    ledger = repo.get_hunger_ledger(TASK_ID)

    assert report.verdict is ValidationVerdict.FAIL
    assert pytest_assertion.status == "failed"
    assert pytest_assertion.evidence_ids == ["ev-pytest"]
    assert len(ledger.items) == 1
    assert "scrutiny_test failed" in ledger.items[0].title
    assert ledger.items[0].generated_by.startswith("WH-task-1-4-scrutiny_validator-")


async def test_failed_assertions_respect_budget_cap(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [
            _sandbox_result(exit_code=1, evidence_id="ev-pytest", stderr="pytest bad"),
            _sandbox_result(exit_code=1, evidence_id="ev-ruff", stderr="ruff bad"),
            _sandbox_result(exit_code=1, evidence_id="ev-mypy", stderr="mypy bad"),
        ],
    )

    await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(max_new_items_per_loop=2),
    )

    assert len(repo.get_hunger_ledger(TASK_ID).items) == 2


async def test_timeout_blocks_assertion(tmp_path: Path) -> None:
    long_stderr = "timeout stderr " * 600
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [
            _sandbox_result(
                exit_code=-9,
                evidence_id="ev-pytest",
                timed_out=True,
                stderr=long_stderr,
            ),
            _sandbox_result(evidence_id="ev-ruff"),
            _sandbox_result(evidence_id="ev-mypy"),
        ],
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertions = repo.list_validation_assertions(
        mission_id=MISSION_ID,
        phase_id=PHASE_ID,
    )
    pytest_assertion = next(
        assertion for assertion in assertions if assertion.check_type == "scrutiny_test"
    )
    timeout_events = repo.list_events(
        TASK_ID,
        event_types=["validation.scrutiny_timeout"],
    )

    assert report.verdict is ValidationVerdict.FAIL
    assert pytest_assertion.status == "blocked"
    assert pytest_assertion.evidence_ids == ["ev-pytest"]
    assert repo.get_hunger_ledger(TASK_ID).items == []
    assert len(timeout_events) == 1
    assert timeout_events[0]["payload"]["argv"] == PYTEST_ARGV
    assert timeout_events[0]["payload"]["phase_id"] == PHASE_ID
    assert len(str(timeout_events[0]["payload"]["stderr"])) <= 5000


async def test_pipeline_owned_lifecycle_events_are_not_duplicated(
    tmp_path: Path,
) -> None:
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        _success_results(),
    )
    repo.append_event(
        "validation.scrutiny_started",
        {"phase_id": PHASE_ID},
        task_id=TASK_ID,
        loop_id=LOOP_ID,
    )

    await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert (
        len(repo.list_events(TASK_ID, event_types=["validation.scrutiny_started"]))
        == 1
    )
    assert repo.list_events(TASK_ID, event_types=["validation.scrutiny_completed"]) == []


def test_path_safety_only() -> None:
    source = Path(
        "src/hungerloop/services/validators/scrutiny_validator.py"
    ).read_text(encoding="utf-8")
    assert "resolve_workspace_path" in source


def test_no_unsafe_subprocess_patterns() -> None:
    source = Path(
        "src/hungerloop/services/validators/scrutiny_validator.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "shell=True",
        "os.system(",
        'subprocess.run("',
        'subprocess.Popen("',
    ):
        assert forbidden not in source
