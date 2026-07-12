"""Post-commit synthesis backfill efficiency tests."""
from __future__ import annotations

from pathlib import Path
from typing import cast

from hungerloop.models.events import EventType
from hungerloop.models.hunger import HungerLedger, HungerPolicy
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.loop_orchestrator import LoopOrchestrator
from hungerloop.services.synthesized_check_lifecycle import (
    BaselineValidationResult,
    SynthesizedCheckLifecycle,
)
from hungerloop.services.workspace_manager import WorkspaceManager


class _FakeSynthesizer:
    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.call_count = 0

    async def synthesize_post_commit(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission_prose: str,
        feature_descriptions: list[str],
        synthesis_max_total_items: int,
        synthesis_max_active_items: int,
        synthesis_batch_size: int,
        synthesis_audit_enabled: bool,
        covered_check_digest: str | None,
        dry_run_cwd: Path | None = None,
        existing_dedup_keys: set[str] | None = None,
    ) -> list[str]:
        del (
            task_id,
            loop_id,
            mission_prose,
            feature_descriptions,
            synthesis_max_total_items,
            synthesis_max_active_items,
            synthesis_batch_size,
            synthesis_audit_enabled,
            covered_check_digest,
            dry_run_cwd,
            existing_dedup_keys,
        )
        self.call_count += 1
        return self.batches.pop(0) if self.batches else []


class _FakeLifecycle:
    def __init__(self, results: list[BaselineValidationResult]) -> None:
        self.results = list(results)
        self.call_count = 0

    async def validate_pending_baseline(
        self,
        *,
        task_id: str,
        loop_id: int,
        workspace_root: Path | None = None,
    ) -> BaselineValidationResult:
        del task_id, loop_id, workspace_root
        self.call_count += 1
        return self.results.pop(0)


def _orchestrator(
    tmp_path: Path,
    *,
    synth: _FakeSynthesizer,
    lifecycle: _FakeLifecycle,
) -> tuple[LoopOrchestrator, InMemoryRepository]:
    repo = InMemoryRepository()
    repo.create_task("t1", "goal")
    repo.save_hunger_ledger("t1", HungerLedger(task_id="t1"))
    orchestrator = object.__new__(LoopOrchestrator)
    orchestrator.repo = repo
    orchestrator.workspace_manager = WorkspaceManager(tmp_path)
    orchestrator.spec_check_synthesizer = synth
    orchestrator.synthesized_check_lifecycle = cast(
        SynthesizedCheckLifecycle,
        lifecycle,
    )
    return orchestrator, repo


async def test_zero_actionable_batch_stops_same_commit_backfill(
    tmp_path: Path,
) -> None:
    synth = _FakeSynthesizer([["H-SYN-001"], ["H-SYN-002"]])
    lifecycle = _FakeLifecycle(
        [
            BaselineValidationResult(
                attempted_item_ids=["H-SYN-001"],
                auto_satisfied_item_ids=["H-SYN-001"],
                failed_item_ids=[],
                rejected_fixture_item_ids=[],
            )
        ]
    )
    orchestrator, repo = _orchestrator(
        tmp_path,
        synth=synth,
        lifecycle=lifecycle,
    )

    await orchestrator._run_post_commit_synthesis(
        task_id="t1",
        loop_id=1,
        policy=HungerPolicy(
            synthesis_max_total_items=3,
            synthesis_batch_size=1,
        ),
        mission=None,
    )

    assert synth.call_count == 1
    assert lifecycle.call_count == 1
    events = repo.list_events(
        "t1",
        event_types=[EventType.SYNTHESIS_BACKFILL_STOPPED.value],
    )
    assert len(events) == 1
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["reason"] == "zero_actionable_yield"
    assert payload["auto_satisfied_item_ids"] == ["H-SYN-001"]


async def test_actionable_batch_allows_one_more_backfill_round(
    tmp_path: Path,
) -> None:
    synth = _FakeSynthesizer(
        [["H-SYN-001"], ["H-SYN-002"], ["H-SYN-003"]]
    )
    lifecycle = _FakeLifecycle(
        [
            BaselineValidationResult(
                attempted_item_ids=["H-SYN-001"],
                auto_satisfied_item_ids=[],
                failed_item_ids=["H-SYN-001"],
                rejected_fixture_item_ids=[],
            ),
            BaselineValidationResult(
                attempted_item_ids=["H-SYN-002"],
                auto_satisfied_item_ids=["H-SYN-002"],
                failed_item_ids=[],
                rejected_fixture_item_ids=[],
            ),
        ]
    )
    orchestrator, repo = _orchestrator(
        tmp_path,
        synth=synth,
        lifecycle=lifecycle,
    )

    await orchestrator._run_post_commit_synthesis(
        task_id="t1",
        loop_id=1,
        policy=HungerPolicy(
            synthesis_max_total_items=3,
            synthesis_batch_size=1,
        ),
        mission=None,
    )

    assert synth.call_count == 2
    assert lifecycle.call_count == 2
    events = repo.list_events(
        "t1",
        event_types=[EventType.SYNTHESIS_BACKFILL_STOPPED.value],
    )
    assert len(events) == 1
