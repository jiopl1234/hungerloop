"""ValidationGate tests with the *real* AcceptanceCheckRunner.

`test_targeted_validation.py` exists but mocks the runner via
``MagicMock(spec=AcceptanceCheckRunner)``, so the gate's interaction with
real check execution, evidence wiring, and verdict edges (acceptance_mode
"any", target item with no checks, PARTIAL fallback) was never exercised.
These tests pin those branches end-to-end against `InMemoryRepository` +
`WorkspaceManager` + `SandboxRunner`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    HungerItemType,
    ValidationVerdict,
)
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner
from hungerloop.services.sandbox_runner import SandboxRunner
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.workspace_manager import WorkspaceManager


@pytest.fixture
def gate_setup(
    tmp_path: Path,
) -> tuple[ValidationGate, InMemoryRepository, WorkspaceManager]:
    repo = InMemoryRepository()
    wm = WorkspaceManager(tmp_path)
    wm.ensure_task_workspace("t1")
    wm.create_candidate_workspace("t1", 1)
    sb = SandboxRunner(repo)
    runner = AcceptanceCheckRunner(repo, wm, sb)
    return ValidationGate(repo=repo, acceptance_runner=runner), repo, wm


def _candidate(evidence: list[str] | None = None) -> CandidateState:
    return CandidateState(
        id="CAND-t1-1",
        task_id="t1",
        loop_id=1,
        summary="test",
        workspace_ref="candidates/loop_001",
        evidence_ids=evidence or [],
    )


def _file_exists_item(
    item_id: str, paths: list[str], mode: str = "all"
) -> HungerItem:
    return HungerItem(
        id=item_id,
        title=f"Item {item_id}",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": p},
            )
            for p in paths
        ],
        acceptance_mode=mode,  # type: ignore[arg-type]
    )


# ---- happy path with real runner --------------------------------------------


async def test_real_runner_full_pass_emits_verdict_pass(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """Two checks, both produce real files in the candidate workspace ->
    PASS, both check keys land in newly_passed."""
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")
    (cand_root / "b.md").write_text("b")

    item = _file_exists_item("H-001", ["a.md", "b.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.verdict == ValidationVerdict.PASS
    assert sorted(report.newly_passed_check_keys) == ["H-001:0", "H-001:1"]
    assert report.satisfied_hunger_item_ids == ["H-001"]
    assert report.unsatisfied_hunger_item_ids == []


async def test_real_runner_partial_pass_emits_verdict_partial(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """One file present, one missing -> PARTIAL with one newly_passed and
    one unsatisfied target item (acceptance_mode='all' requires both)."""
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")
    # b.md NOT created.

    item = _file_exists_item("H-001", ["a.md", "b.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.verdict == ValidationVerdict.PARTIAL
    assert "H-001:0" in report.newly_passed_check_keys
    assert "H-001:1" not in report.newly_passed_check_keys
    assert "H-001" in report.unsatisfied_hunger_item_ids


# ---- acceptance_mode="any" ---------------------------------------------------


async def test_acceptance_mode_any_satisfied_when_one_check_passes(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """acceptance_mode='any' satisfies the item as soon as one check passes —
    this branch was untested before."""
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")
    # b.md missing — but mode='any' so item is still satisfied.

    item = _file_exists_item("H-001", ["a.md", "b.md"], mode="any")
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.verdict == ValidationVerdict.PASS
    assert report.satisfied_hunger_item_ids == ["H-001"]


# ---- target item with no acceptance checks ----------------------------------


async def test_target_item_with_no_checks_is_unsatisfied(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """A target item with zero acceptance_checks produces no CheckResults
    -> unsatisfied (covers the `if not results: unsatisfied` branch)."""
    gate, repo, _wm = gate_setup
    item = HungerItem(
        id="H-001",
        title="empty",
        item_type=HungerItemType.GOAL_GAP,
        acceptance_checks=[],
    )
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.unsatisfied_hunger_item_ids == ["H-001"]
    assert report.satisfied_hunger_item_ids == []
    # No newly_passed and no regressed -> FAIL fallback verdict.
    assert report.verdict == ValidationVerdict.FAIL


# ---- regression detection (real runner) -------------------------------------


async def test_regression_against_baseline_emits_fail(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """Best-state has H-001:0 accepted; the file is missing in the candidate
    -> regression -> FAIL even if other targets pass."""
    gate, repo, wm = gate_setup
    # Candidate workspace lacks report.md.
    item = _file_exists_item("H-001", ["report.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-001:0"],
        )
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.regressed_check_keys == ["H-001:0"]
    assert report.verdict == ValidationVerdict.FAIL
    # Regression descriptions should be human-readable.
    assert any("regressed" in r for r in report.regressions)


async def test_regression_confirm_rerun_converts_flaky_failure_to_pass(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, _ = gate_setup
    repo.set_hunger_policy("t1", HungerPolicy(regression_confirm_reruns=2))
    item = _file_exists_item("H-001", ["report.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-001:0"],
        )
    )
    gate.runner.run = AsyncMock(
        side_effect=[
            (False, "initial failure", "ev-initial"),
            (True, "confirmation pass 1", "ev-confirm-1"),
            (True, "confirmation pass 2", "ev-confirm-2"),
        ]
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.verdict == ValidationVerdict.PASS
    assert report.regressed_check_keys == []
    assert report.check_results[0].passed is True
    assert "flaky: passed all 2 confirmation reruns" in report.check_results[0].detail
    assert report.evidence_ids == ["ev-initial", "ev-confirm-1", "ev-confirm-2"]
    events = repo.list_events(
        "t1",
        event_types=[EventType.CHECK_REGRESSION_DISCONFIRMED.value],
    )
    assert len(events) == 1
    assert events[0]["payload"]["check_key"] == "H-001:0"
    assert events[0]["payload"]["confirmation_evidence_ids"] == [
        "ev-confirm-1",
        "ev-confirm-2",
    ]


async def test_regression_confirm_rerun_keeps_persistent_failure(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, _ = gate_setup
    repo.set_hunger_policy("t1", HungerPolicy(regression_confirm_reruns=2))
    item = _file_exists_item("H-001", ["report.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-001:0"],
        )
    )
    gate.runner.run = AsyncMock(
        side_effect=[
            (False, "initial failure", "ev-initial"),
            (False, "confirmation failure 1", "ev-confirm-1"),
            (False, "confirmation failure 2", "ev-confirm-2"),
        ]
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.verdict == ValidationVerdict.FAIL
    assert report.regressed_check_keys == ["H-001:0"]
    assert report.evidence_ids == ["ev-initial", "ev-confirm-1", "ev-confirm-2"]
    assert repo.list_events(
        "t1",
        event_types=[EventType.CHECK_REGRESSION_DISCONFIRMED.value],
    ) == []


async def test_regression_confirm_requires_every_rerun_to_pass(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, _ = gate_setup
    repo.set_hunger_policy("t1", HungerPolicy(regression_confirm_reruns=2))
    item = _file_exists_item("H-001", ["report.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-001:0"],
        )
    )
    gate.runner.run = AsyncMock(
        side_effect=[
            (False, "initial failure", "ev-initial"),
            (True, "confirmation pass", "ev-confirm-1"),
            (False, "confirmation failure", "ev-confirm-2"),
        ]
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.verdict == ValidationVerdict.FAIL
    assert report.regressed_check_keys == ["H-001:0"]
    assert gate.runner.run.await_count == 3
    assert repo.list_events(
        "t1",
        event_types=[EventType.CHECK_REGRESSION_DISCONFIRMED.value],
    ) == []


async def test_regression_confirm_rerun_can_be_disabled(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, _ = gate_setup
    repo.set_hunger_policy("t1", HungerPolicy(regression_confirm_reruns=0))
    item = _file_exists_item("H-001", ["report.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-001:0"],
        )
    )
    gate.runner.run = AsyncMock(
        return_value=(False, "initial failure", "ev-initial")
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(),
        target_hunger_item_ids=["H-001"],
    )

    assert report.regressed_check_keys == ["H-001:0"]
    assert gate.runner.run.await_count == 1


# ---- evidence wiring --------------------------------------------------------


async def test_no_evidence_anywhere_emits_fail_with_missing_evidence(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """Candidate has no evidence_ids and FILE_EXISTS produces no evidence
    either; the gate must fail with `missing_evidence` populated."""
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")

    item = _file_exists_item("H-001", ["a.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=[]),  # no evidence
        target_hunger_item_ids=["H-001"],
    )
    assert report.verdict == ValidationVerdict.FAIL
    assert report.missing_evidence  # non-empty


# ---- previously-passed-untested -> currently_passed -------------------------


async def test_previously_passed_check_not_targeted_stays_passed(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    """I-5 corollary: a previously-passed check from a *different* item
    that isn't a target *and* isn't returned as a regression candidate
    should still appear in currently_passed_check_keys (untested checks
    stay passed). This pins the `currently_passed = newly ∪ untested_prev`
    branch."""
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "target.md").write_text("t")

    target = _file_exists_item("H-001", ["target.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[target]))
    repo.save_hunger_item(target)

    # H-002:0 was passed in baseline but the item itself is not in the
    # repo's items map, so get_items_for_check_keys returns []; the gate
    # must still carry "H-002:0" through to currently_passed_check_keys.
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-002:0"],
        )
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert "H-002:0" in report.currently_passed_check_keys
    assert "H-001:0" in report.currently_passed_check_keys


async def test_targets_only(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    for filename in ["target.md", "regression.md", "ignored.md"]:
        (cand_root / filename).write_text("ok", encoding="utf-8")

    target = _file_exists_item("H-target", ["target.md"])
    previous = _file_exists_item("H-prev", ["regression.md"])
    ignored = _file_exists_item("H-ignored", ["ignored.md"])
    repo.save_hunger_ledger(
        "t1",
        HungerLedger(task_id="t1", items=[target, previous, ignored]),
    )
    for item in (target, previous, ignored):
        repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(
            task_id="t1",
            state_id="prev",
            summary="prev",
            accepted_check_keys=["H-prev:0"],
        )
    )
    executed: list[str] = []
    real_run = gate.runner.run

    async def recording_run(**kwargs: Any) -> tuple[bool, str, str | None]:
        check = kwargs["check"]
        executed.append(str(check.params["path"]))
        return await real_run(**kwargs)

    monkeypatch.setattr(gate.runner, "run", recording_run)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-target"],
    )

    assert executed == ["target.md", "regression.md"]
    assert report.attempted_hunger_item_ids == ["H-target"]
    assert "H-prev:0" in report.currently_passed_check_keys
    assert "H-ignored:0" not in report.currently_passed_check_keys


# ---- baseline_state_id wiring ------------------------------------------------


async def test_baseline_state_id_carries_through_when_best_exists(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")

    item = _file_exists_item("H-001", ["a.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)
    repo.save_best_state(
        BestState(task_id="t1", state_id="STATE-prev", summary="prev")
    )

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.baseline_state_id == "STATE-prev"


async def test_baseline_state_id_is_none_when_no_best(
    gate_setup: tuple[ValidationGate, InMemoryRepository, WorkspaceManager],
) -> None:
    gate, repo, wm = gate_setup
    cand_root = wm.candidate_files_dir("t1", 1)
    (cand_root / "a.md").write_text("a")

    item = _file_exists_item("H-001", ["a.md"])
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1", items=[item]))
    repo.save_hunger_item(item)

    report = await gate.validate(
        task_id="t1",
        loop_id=1,
        candidate=_candidate(evidence=["seed-ev"]),
        target_hunger_item_ids=["H-001"],
    )
    assert report.baseline_state_id is None
