from __future__ import annotations

from pathlib import Path

from hungerloop.models.hunger import HungerLedger
from hungerloop.models.tracing import LoopTrace
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.workspace_manager import WorkspaceManager
from tests.integration.test_mission_run_single_worker import (
    TASK_ID,
    _build_orchestrator,
    _item,
    _mission,
    _RecordingWorker,
    _save_mission,
    _seed_policy,
)


async def test_disjoint_writes_both_committed(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    _seed_policy(repo)
    repo.save_hunger_ledger(
        TASK_ID,
        HungerLedger(
            task_id=TASK_ID,
            items=[_item("H-1", "src/a.py"), _item("H-2", "src/b.py")],
        ),
    )
    _save_mission(repo, _mission(["F-1", "F-2"], max_parallel_features=2))
    worker = _RecordingWorker(
        {
            "F-1": ("src/a.py", "A = 1\n"),
            "F-2": ("src/b.py", "B = 2\n"),
        }
    )
    orchestrator = _build_orchestrator(
        repo=repo,
        tmp_path=tmp_path,
        worker=worker,
        max_workers_per_loop=2,
    )

    outcome = await orchestrator.step(TASK_ID)

    assert isinstance(outcome, LoopTrace)
    assert outcome.committed is True
    best = WorkspaceManager(tmp_path).best_files_dir(TASK_ID)
    assert (best / "src" / "a.py").read_text(encoding="utf-8") == "A = 1\n"
    assert (best / "src" / "b.py").read_text(encoding="utf-8") == "B = 2\n"
    assert len(repo.list_events(TASK_ID, event_types=["mission.feature_assigned"])) == 2
    assert len(repo.list_events(TASK_ID, event_types=["mission.feature_completed"])) == 2
    assert repo.list_events(TASK_ID, event_types=["WORKSPACE_WRITE_COLLISION"]) == []
    assert [ctx.target_feature_ids for ctx in worker.contexts] == [["F-1"], ["F-2"]]
    assert len(outcome.assignment_traces) == 2
