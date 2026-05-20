from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest

from hungerloop.models.blackboard import CandidateState
from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.hunger import AcceptanceCheck, HungerItem, HungerLedger
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.validation_gate import ValidationGate
from hungerloop.services.validators.deterministic_validator import DeterministicValidator

TASK_ID = "task-1"
LOOP_ID = 1
ITERATIONS = 100
_MIN_WORK_SECONDS = 0.003
_TIMER_FLOOR_SECONDS = 0.0005


class _CpuBoundRunner:
    def __init__(self) -> None:
        self.work_iterations = self._calibrate_work_iterations()

    async def run(
        self,
        *,
        check: AcceptanceCheck,
        task_id: str,
        loop_id: int,
        candidate: CandidateState,
    ) -> tuple[bool, str, str]:
        del check, task_id, loop_id, candidate
        accumulator = 0
        for index in range(self.work_iterations):
            accumulator += index * index
        return True, "ok", f"ev-{accumulator % 97}"

    @staticmethod
    def _calibrate_work_iterations() -> int:
        iterations = 8_000
        while True:
            started = time.perf_counter()
            accumulator = 0
            for index in range(iterations):
                accumulator += index * index
            elapsed = time.perf_counter() - started
            if elapsed >= _MIN_WORK_SECONDS:
                return iterations
            iterations *= 2


def _repo() -> InMemoryRepository:
    repo = InMemoryRepository()
    item = HungerItem(
        id="H-001",
        title="perf item",
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=AcceptanceCheckType.FILE_EXISTS,
                params={"path": "perf.txt"},
            )
        ],
    )
    repo.save_hunger_ledger(TASK_ID, HungerLedger(task_id=TASK_ID, items=[item]))
    repo.save_hunger_item(item)
    return repo


def _candidate(tmp_path: Path) -> CandidateState:
    return CandidateState(
        id="CAND-task-1-1",
        task_id=TASK_ID,
        loop_id=LOOP_ID,
        summary="candidate",
        workspace_ref=str(tmp_path),
        evidence_ids=["candidate-ev"],
    )


async def _measure(callable_obj: Any) -> list[float]:
    durations: list[float] = []
    for _ in range(ITERATIONS):
        started = time.perf_counter()
        await callable_obj()
        durations.append(time.perf_counter() - started)
    return durations


@pytest.mark.perf
async def test_deterministic_validator_overhead_within_one_percent(
    tmp_path: Path,
) -> None:
    repo = _repo()
    candidate = _candidate(tmp_path)
    gate = ValidationGate(repo, _CpuBoundRunner())  # type: ignore[arg-type]
    wrapper = DeterministicValidator(gate)

    async def run_gate() -> object:
        return await gate.validate(TASK_ID, LOOP_ID, candidate, ["H-001"])

    async def run_wrapper() -> object:
        return await wrapper.validate(TASK_ID, LOOP_ID, candidate, ["H-001"])

    # Warm up caches and adaptive interpreter specialization before sampling.
    await run_gate()
    await run_wrapper()

    direct_median = statistics.median(await _measure(run_gate))
    wrapper_median = statistics.median(await _measure(run_wrapper))
    allowed = (direct_median * 1.01) + _TIMER_FLOOR_SECONDS

    assert wrapper_median <= allowed, (
        "DeterministicValidator overhead exceeded 1% timer-adjusted budget: "
        f"direct={direct_median:.6f}s wrapper={wrapper_median:.6f}s "
        f"allowed={allowed:.6f}s"
    )
