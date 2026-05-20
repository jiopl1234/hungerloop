from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import LoopPhase, ValidationVerdict
from hungerloop.models.hunger import HungerLedger
from hungerloop.models.mission import Mission, MissionPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.sandbox_runner import SandboxRunResult
from hungerloop.services.validators.user_testing_validator import UserTestingValidator
from hungerloop.services.workspace_manager import WorkspaceManager

TASK_ID = "task-1"
LOOP_ID = 4
MISSION_ID = "mission-1"
PHASE_ID = "phase-1"
OTHER_PHASE_ID = "phase-2"


class _RecordingSandboxRunner:
    def __init__(self, results: list[SandboxRunResult] | None = None) -> None:
        self.results = list(results or [])
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
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
    evidence_id: str = "shell-ev",
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


def _candidate(evidence_ids: list[str] | None = None) -> CandidateState:
    return CandidateState(
        id="CAND-task-1-4",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate summary",
        workspace_ref=f"candidates/loop_{LOOP_ID:03d}",
        evidence_ids=list(evidence_ids or ["candidate-ev"]),
    )


def _phase(
    phase_id: str = PHASE_ID,
    *,
    status: str = "validating",
) -> MissionPhase:
    return MissionPhase(
        phase_id=phase_id,
        title=phase_id,
        description=f"{phase_id} description",
        status=status,  # type: ignore[arg-type]
    )


def _assertion(
    assertion_id: str,
    *,
    phase_id: str = PHASE_ID,
    check_type: str = "file_contains_regex",
    params: dict[str, object] | None = None,
    evidence_requirements: list[str] | None = None,
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=assertion_id,
        phase_id=phase_id,
        title=assertion_id,
        description=f"{assertion_id} description",
        check_type=check_type,
        params=dict(params or {}),
        evidence_requirements=list(evidence_requirements or []),
    )


def _budget() -> BudgetAllocation:
    return BudgetAllocation(phase=LoopPhase.EXPLORE)


def _setup(
    tmp_path: Path,
    assertions: list[ValidationAssertion],
    *,
    sandbox: _RecordingSandboxRunner | None = None,
) -> tuple[
    UserTestingValidator,
    InMemoryRepository,
    _RecordingSandboxRunner,
    CandidateState,
    ValidationContract,
    MissionPhase,
    Path,
]:
    repo = InMemoryRepository()
    repo.create_task(TASK_ID, "Run user testing")
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[]))
    phase = _phase()
    other_phase = _phase(OTHER_PHASE_ID, status="pending")
    mission = Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="Mission",
        description="Mission description",
        phases=[phase, other_phase],
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    repo.save_mission(mission)
    repo.save_mission_phase(phase)
    repo.save_mission_phase(other_phase)
    contract = ValidationContract(mission_id=MISSION_ID, assertions=assertions)
    repo.save_validation_contract(contract)

    workspace_manager = WorkspaceManager(tmp_path)
    candidate_files = workspace_manager.candidate_files_dir(TASK_ID, LOOP_ID)
    candidate_files.mkdir(parents=True, exist_ok=True)
    runner = sandbox or _RecordingSandboxRunner()
    validator = UserTestingValidator(
        repo=repo,
        sandbox_runner=runner,
        workspace_manager=workspace_manager,
    )
    return validator, repo, runner, _candidate(), contract, phase, candidate_files


async def test_passes_only_phase_assertions(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion("A1", params={"path": "one.txt", "regex": "ok"}),
            _assertion("A2", params={"path": "two.txt", "regex": "ok"}),
            _assertion("A3", params={"path": "three.txt", "regex": "ok"}),
            _assertion(
                "B1",
                phase_id=OTHER_PHASE_ID,
                params={"path": "missing.txt", "regex": "never"},
            ),
            _assertion(
                "B2",
                phase_id=OTHER_PHASE_ID,
                params={"path": "missing.txt", "regex": "never"},
            ),
        ],
    )
    for name in ("one.txt", "two.txt", "three.txt"):
        (candidate_files / name).write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert report.verdict is ValidationVerdict.PASS
    assert len(report.evidence_ids) == 3
    p1_assertions = repo.list_validation_assertions(
        mission_id=MISSION_ID,
        phase_id=PHASE_ID,
    )
    p2_assertions = repo.list_validation_assertions(
        mission_id=MISSION_ID,
        phase_id=OTHER_PHASE_ID,
    )
    assert [assertion.status for assertion in p1_assertions] == [
        "passed",
        "passed",
        "passed",
    ]
    assert [assertion.status for assertion in p2_assertions] == ["pending", "pending"]


async def test_unknown_check_type(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [_assertion("A1", check_type="quantum_judge")],
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertion = repo.list_validation_assertions(mission_id=MISSION_ID)[0]
    evidence = repo._evidence[assertion.evidence_ids[0]]
    assert report.verdict is ValidationVerdict.FAIL
    assert assertion.status == "failed"
    assert evidence["detail"] == "unknown_check_type"
    assert repo.list_events(TASK_ID, event_types=["validation.user_testing_started"])


async def test_failed_assertion_with_detail(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion(
                "A1",
                check_type="behavioral_assertion",
                params={"file": "x.md", "headers": ["## Foo"]},
            )
        ],
    )
    content = "intro " + ("x" * 260)
    (candidate_files / "x.md").write_text(content, encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertion = repo.list_validation_assertions(mission_id=MISSION_ID)[0]
    evidence = repo._evidence[assertion.evidence_ids[0]]
    assert report.verdict is ValidationVerdict.FAIL
    assert assertion.status == "failed"
    assert "missing_header:## Foo" in str(evidence["detail"])
    assert content[:200] in str(evidence["detail"])


async def test_predicate_timeout_blocks(tmp_path: Path) -> None:
    sandbox = _RecordingSandboxRunner(
        [
            _sandbox_result(
                stdout="partial",
                stderr="timeout",
                exit_code=-9,
                timed_out=True,
                evidence_id="shell-timeout-ev",
            )
        ]
    )
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [
            _assertion(
                "A1",
                check_type="command_stdout_contains",
                params={"argv": ["python", "-c", "while True: pass"], "contains": "ok"},
            )
        ],
        sandbox=sandbox,
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertion = repo.list_validation_assertions(mission_id=MISSION_ID)[0]
    timeout_events = repo.list_events(
        TASK_ID,
        event_types=["validation.user_testing_timeout"],
    )
    assert report.verdict is ValidationVerdict.PARTIAL
    assert assertion.status == "blocked"
    assert assertion.evidence_ids[1:] == ["shell-timeout-ev"]
    assert assertion.evidence_ids[0] in report.evidence_ids
    assert len(timeout_events) == 1
    assert timeout_events[0]["payload"]["argv"] == [
        "python",
        "-c",
        "while True: pass",
    ]


async def test_verdict_pass(tmp_path: Path) -> None:
    validator, _repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion("A1", params={"path": "a.txt", "regex": "ok"}),
            _assertion("A2", params={"path": "b.txt", "regex": "ok"}),
        ],
    )
    (candidate_files / "a.txt").write_text("ok", encoding="utf-8")
    (candidate_files / "b.txt").write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert report.verdict is ValidationVerdict.PASS


async def test_verdict_fail(tmp_path: Path) -> None:
    validator, _repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion("A1", params={"path": "a.txt", "regex": "ok"}),
            _assertion("A2", params={"path": "b.txt", "regex": "missing"}),
        ],
    )
    (candidate_files / "a.txt").write_text("ok", encoding="utf-8")
    (candidate_files / "b.txt").write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert report.verdict is ValidationVerdict.FAIL


async def test_missing_evidence_fails(tmp_path: Path) -> None:
    validator, _repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion(
                "A1",
                params={
                    "path": "a.txt",
                    "regex": "ok",
                    "required_evidence_ids": ["required-external-evidence"],
                },
            )
        ],
    )
    (candidate_files / "a.txt").write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert report.verdict is ValidationVerdict.FAIL
    assert report.missing_evidence == ["required-external-evidence"]


async def test_persists_evidence(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion("A1", params={"path": "a.txt", "regex": "ok"}),
            _assertion("A2", params={"path": "b.txt", "regex": "ok"}),
            _assertion("A3", params={"path": "c.txt", "regex": "ok"}),
        ],
    )
    for filename in ("a.txt", "b.txt", "c.txt"):
        (candidate_files / filename).write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert len(report.evidence_ids) == 3
    assert (
        sum(
            1
            for row in repo._evidence.values()
            if row.get("evidence_kind") == "user_testing_predicate"
        )
        == 3
    )
    assertions = repo.list_validation_assertions(mission_id=MISSION_ID)
    assert all(assertion.evidence_ids for assertion in assertions)


async def test_verdict_partial_when_blocked_without_failures(tmp_path: Path) -> None:
    sandbox = _RecordingSandboxRunner(
        [
            _sandbox_result(
                timed_out=True,
                exit_code=-9,
                evidence_id="shell-timeout-ev",
            )
        ]
    )
    validator, _repo, _sandbox, candidate, contract, phase, candidate_files = _setup(
        tmp_path,
        [
            _assertion("A1", params={"path": "a.txt", "regex": "ok"}),
            _assertion(
                "A2",
                check_type="command_stdout_contains",
                params={"argv": ["python", "-c", "sleep"], "contains": "never"},
            ),
        ],
        sandbox=sandbox,
    )
    (candidate_files / "a.txt").write_text("ok", encoding="utf-8")

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assert report.verdict is ValidationVerdict.PARTIAL


async def test_malformed_params(tmp_path: Path) -> None:
    validator, repo, _sandbox, candidate, contract, phase, _candidate_files = _setup(
        tmp_path,
        [_assertion("A1", check_type="behavioral_assertion", params={})],
    )

    report = await validator.validate(
        TASK_ID,
        LOOP_ID,
        candidate,
        contract=contract,
        phase=phase,
        budget=_budget(),
    )

    assertion = repo.list_validation_assertions(mission_id=MISSION_ID)[0]
    evidence = repo._evidence[assertion.evidence_ids[0]]
    assert report.verdict is ValidationVerdict.FAIL
    assert assertion.status == "failed"
    assert evidence["detail"] == "malformed_params"
