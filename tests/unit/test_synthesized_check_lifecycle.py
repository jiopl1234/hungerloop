"""Regression tests for synthesized-check baseline and conflict lifecycle."""
from __future__ import annotations

from pathlib import Path

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemStatus,
    ValidationVerdict,
)
from hungerloop.models.events import EventType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.validation import CheckResult, ValidationReport
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.hunger_update import HungerUpdateService
from hungerloop.services.proposal_dedup import collect_rejected_proposal_dedup_keys
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.synthesized_check_lifecycle import SynthesizedCheckLifecycle
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.workspace_manager import WorkspaceManager


def _lifecycle(
    tmp_path: Path,
) -> tuple[
    SynthesizedCheckLifecycle,
    InMemoryRepository,
    WorkspaceManager,
    RefinementCompiler,
]:
    repo = InMemoryRepository()
    workspace_manager = WorkspaceManager(tmp_path)
    workspace_manager.ensure_task_workspace("t1")
    runner = AcceptanceCheckRunner(
        repo,
        workspace_manager,
        SandboxRunner(repo),
    )
    compiler = RefinementCompiler(repo)
    lifecycle = SynthesizedCheckLifecycle(
        repo=repo,
        validation_gate=ValidationGate(repo, runner),
        workspace_manager=workspace_manager,
        hunger_update=HungerUpdateService(repo),
        refinement_compiler=compiler,
    )
    return lifecycle, repo, workspace_manager, compiler


def _seed_best(
    repo: InMemoryRepository,
    workspace_manager: WorkspaceManager,
) -> Path:
    best_root = workspace_manager.best_files_dir("t1")
    best_root.mkdir(parents=True, exist_ok=True)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="BEST-t1-1",
            summary="baseline",
            updated_at_loop=1,
        )
    )
    return best_root


async def test_baseline_pass_auto_satisfies_without_worker(
    tmp_path: Path,
) -> None:
    lifecycle, repo, workspace_manager, compiler = _lifecycle(tmp_path)
    best_root = _seed_best(repo, workspace_manager)
    (best_root / "ready.txt").write_text("ready", encoding="utf-8")
    compiler.compile_spec_coverage(
        task_id="t1",
        proposals=[
            CheckProposal(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "ready.txt"},
                description="ready file exists",
                source_quote="ready file exists",
                proposed_by="synthesizer",
            )
        ],
        generated_by="synthesizer",
        baseline_pending=True,
    )

    result = await lifecycle.validate_pending_baseline(task_id="t1", loop_id=2)

    item = repo.get_hunger_ledger("t1").items[0]
    best = repo.get_best_state("t1")
    assert result.auto_satisfied_item_ids == ["H-SYN-001"]
    assert item.status == HungerItemStatus.VALIDATED_SATISFIED
    assert item.gap_score == 0
    assert item.synthesis_baseline_pending is False
    assert best is not None
    assert best.accepted_check_keys == ["H-SYN-001:0"]
    events = repo.list_events(
        "t1",
        event_types=[EventType.SYNTH_CHECK_AUTO_SATISFIED.value],
    )
    assert len(events) == 1


async def test_failing_fixture_is_closed_and_rejection_is_sticky(
    tmp_path: Path,
) -> None:
    lifecycle, repo, workspace_manager, compiler = _lifecycle(tmp_path)
    _seed_best(repo, workspace_manager)
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={"argv": ["python", "-c", "print('assertion')"]},
        description="fixture must build the example",
        source_quote="fixture must build the example",
        proposed_by="synthesizer",
        fixture_argv=[
            "python",
            "-c",
            "from pathlib import Path; Path('missing/child').write_text('x')",
        ],
    )
    compiler.compile_spec_coverage(
        task_id="t1",
        proposals=[proposal],
        generated_by="synthesizer",
        baseline_pending=True,
    )

    result = await lifecycle.validate_pending_baseline(task_id="t1", loop_id=2)

    item = repo.get_hunger_ledger("t1").items[0]
    assert result.rejected_fixture_item_ids == ["H-SYN-001"]
    assert item.status == HungerItemStatus.CLOSED
    assert item.synthesis_resolution_kind == "invalid_synthesis"
    assert item.synthesis_resolution_reason == "fixture_setup_failed"
    assert collect_rejected_proposal_dedup_keys(repo, "t1") == {
        proposal.dedup_key()
    }


async def test_fixture_and_assertion_share_isolated_workspace(
    tmp_path: Path,
) -> None:
    lifecycle, repo, workspace_manager, compiler = _lifecycle(tmp_path)
    best_root = _seed_best(repo, workspace_manager)
    proposal = CheckProposal(
        check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
        params={
            "argv": [
                "python",
                "-c",
                "from pathlib import Path; "
                "assert Path('fixture.txt').read_text() == 'ready'",
            ]
        },
        description="fixture feeds the assertion",
        source_quote="fixture feeds the assertion",
        proposed_by="synthesizer",
        fixture_argv=[
            "python",
            "-c",
            "from pathlib import Path; Path('fixture.txt').write_text('ready')",
        ],
    )
    compiler.compile_spec_coverage(
        task_id="t1",
        proposals=[proposal],
        generated_by="synthesizer",
        baseline_pending=True,
    )

    result = await lifecycle.validate_pending_baseline(task_id="t1", loop_id=2)

    assert result.auto_satisfied_item_ids == ["H-SYN-001"]
    assert not (best_root / "fixture.txt").exists()
    best = repo.get_best_state("t1")
    assert best is not None
    assert best.evidence_ids


def _conflict_report() -> ValidationReport:
    return ValidationReport(
        id="VAL-t1-2",
        task_id="t1",
        loop_id=2,
        candidate_state_id="CAND-t1-2",
        baseline_state_id="BEST-t1-1",
        verdict=ValidationVerdict.FAIL,
        attempted_hunger_item_ids=["H-SYN-001"],
        check_results=[
            CheckResult(
                hunger_item_id="H-SYN-001",
                check_index=0,
                check_key="H-SYN-001:0",
                check_type=AcceptanceCheckType.FILE_EXISTS,
                passed=True,
                newly_passed=True,
                detail="synth check passed",
            ),
            CheckResult(
                hunger_item_id="H-impl",
                check_index=22,
                check_key="H-impl:22",
                check_type=AcceptanceCheckType.FILE_EXISTS,
                passed=False,
                previously_passed=True,
                regressed=True,
                detail="accepted check regressed",
            ),
        ],
        newly_passed_check_keys=["H-SYN-001:0"],
        regressed_check_keys=["H-impl:22"],
        has_real_progress=True,
    )


def test_repeated_rejected_conflict_quarantines_synthesized_item(
    tmp_path: Path,
) -> None:
    lifecycle, repo, _, _ = _lifecycle(tmp_path)
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-SYN-001",
                    title="conflicting synthesized check",
                    generated_by="synthesizer",
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.FILE_EXISTS,
                            params={"path": "ready.txt"},
                        )
                    ],
                )
            ],
        ),
    )
    report = _conflict_report()

    first = lifecycle.resolve_conflicts(
        task_id="t1",
        loop_id=2,
        validation=report,
        attempted_hunger_item_ids=["H-SYN-001"],
        candidate_committed=False,
        exempted_check_keys=set(),
        threshold=2,
    )
    second = lifecycle.resolve_conflicts(
        task_id="t1",
        loop_id=3,
        validation=report,
        attempted_hunger_item_ids=["H-SYN-001"],
        candidate_committed=False,
        exempted_check_keys=set(),
        threshold=2,
    )

    item = repo.get_hunger_ledger("t1").items[0]
    assert first[0].quarantined is False
    assert second[0].quarantined is True
    assert item.status == HungerItemStatus.CLOSED
    assert item.synthesis_resolution_kind == "invalid_synthesis"
    events = repo.list_events(
        "t1",
        event_types=[EventType.SYNTH_CHECK_QUARANTINED.value],
    )
    assert len(events) == 1


def test_declared_regression_exemption_prevents_false_conflict(
    tmp_path: Path,
) -> None:
    lifecycle, repo, _, _ = _lifecycle(tmp_path)
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-SYN-001",
                    title="refactor-safe synthesized check",
                    generated_by="synthesizer",
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.FILE_EXISTS,
                            params={"path": "ready.txt"},
                        )
                    ],
                )
            ],
        ),
    )

    result = lifecycle.resolve_conflicts(
        task_id="t1",
        loop_id=2,
        validation=_conflict_report(),
        attempted_hunger_item_ids=["H-SYN-001"],
        candidate_committed=False,
        exempted_check_keys={"H-impl:22"},
        threshold=2,
    )

    assert result == []
    assert repo.get_hunger_ledger("t1").items[0].status == HungerItemStatus.OPEN


def test_synthesis_lifecycle_metadata_round_trips_through_sqlite(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "hungerloop.sqlite"
    repo = SQLiteRepository.open(db_path)
    repo.create_task("t1", "goal")
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(
            task_id="t1",
            items=[
                HungerItem(
                    id="H-SYN-001",
                    title="persist lifecycle",
                    generated_by="synthesizer",
                    synthesis_baseline_pending=True,
                    synthesis_fixture_argv=["python", "-c", "print('fixture')"],
                    synthesis_prerequisite_check_keys=["H-001:0"],
                    synthesis_conflict_signatures={"signature": 1},
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.FILE_EXISTS,
                            params={"path": "ready.txt"},
                        )
                    ],
                )
            ],
        ),
    )
    repo.close()

    reopened = SQLiteRepository.open(db_path)
    restored = reopened.get_hunger_ledger("t1").items[0]
    assert restored.synthesis_baseline_pending is True
    assert restored.synthesis_fixture_argv == ["python", "-c", "print('fixture')"]
    assert restored.synthesis_prerequisite_check_keys == ["H-001:0"]
    assert restored.synthesis_conflict_signatures == {"signature": 1}
    reopened.close()
