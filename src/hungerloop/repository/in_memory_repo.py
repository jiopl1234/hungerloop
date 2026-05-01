from __future__ import annotations

import uuid
from typing import Any

from hungerloop.models.blackboard import BestState, CandidateState
from hungerloop.models.enums import LoopPhase
from hungerloop.models.hunger import (
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)
from hungerloop.models.validation import ValidationReport


class InMemoryRepository:
    def __init__(self) -> None:
        self._best_states: dict[str, BestState] = {}
        self._items: dict[str, HungerItem] = {}
        self._policies: dict[str, HungerPolicy] = {}
        self._clocks: dict[str, HungerClockState] = {}
        self._ledgers: dict[str, HungerLedger] = {}
        self._candidates: dict[str, CandidateState] = {}
        self._validation_reports: dict[str, ValidationReport] = {}
        self._snapshots: dict[str, HungerSnapshot] = {}
        self._last_phase: dict[str, LoopPhase] = {}
        self._no_progress_streaks: dict[str, int] = {}
        self._loop_counters: dict[str, int] = {}
        self._evidence: dict[str, dict[str, object]] = {}
        self._events: list[dict[str, object]] = []
        self._approvals: set[str] = set()
        self._committed: set[str] = set()
        self._rejected: set[str] = set()
        self._failures: list[ValidationReport] = []

    def get_best_state(self, task_id: str) -> BestState | None:
        return self._best_states.get(task_id)

    def save_best_state(self, best: BestState) -> None:
        self._best_states[best.task_id] = best

    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]:
        return [self._items[iid] for iid in item_ids if iid in self._items]

    def get_hunger_item(self, item_id: str) -> HungerItem | None:
        return self._items.get(item_id)

    def save_hunger_item(self, item: HungerItem) -> None:
        self._items[item.id] = item

    def get_items_for_check_keys(
        self, task_id: str, check_keys: list[str]
    ) -> list[HungerItem]:
        item_ids = {k.split(":", 1)[0] for k in check_keys}
        return [self._items[iid] for iid in item_ids if iid in self._items]

    def get_hunger_policy(self, task_id: str) -> HungerPolicy:
        return self._policies.get(task_id, HungerPolicy())

    def set_hunger_policy(self, task_id: str, policy: HungerPolicy) -> None:
        self._policies[task_id] = policy

    def get_hunger_clock(self, task_id: str) -> HungerClockState:
        if task_id not in self._clocks:
            self._clocks[task_id] = HungerClockState()
        return self._clocks[task_id]

    def save_hunger_clock(self, clock: HungerClockState) -> None:
        pass

    def get_hunger_ledger(self, task_id: str) -> HungerLedger:
        return self._ledgers.get(
            task_id, HungerLedger(task_id=task_id, items=[])
        )

    def set_hunger_ledger(self, task_id: str, ledger: HungerLedger) -> None:
        self._ledgers[task_id] = ledger
        for item in ledger.items:
            self._items[item.id] = item

    def get_last_phase(self, task_id: str) -> LoopPhase | None:
        return self._last_phase.get(task_id)

    def save_hunger_snapshot(
        self, task_id: str, snapshot: HungerSnapshot
    ) -> None:
        self._snapshots[task_id] = snapshot
        self._last_phase[task_id] = snapshot.phase

    def save_candidate(self, candidate: CandidateState) -> None:
        self._candidates[candidate.id] = candidate

    def mark_candidate_committed(self, candidate_id: str) -> None:
        self._committed.add(candidate_id)

    def mark_candidate_rejected(self, candidate_id: str) -> None:
        self._rejected.add(candidate_id)

    def save_validation_report(self, report: ValidationReport) -> None:
        self._validation_reports[report.id] = report

    def add_failure_from_validation(self, report: ValidationReport) -> None:
        self._failures.append(report)

    def count_evidence_by_type(
        self, task_id: str, evidence_ids: list[str], evidence_type: str
    ) -> int:
        if evidence_type == "any":
            return len(evidence_ids)
        return sum(
            1
            for eid in evidence_ids
            if eid in self._evidence
            and self._evidence[eid].get("type") == evidence_type
        )

    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[Any]:
        return []

    def is_approval_granted(self, approval_id: str) -> bool:
        return approval_id in self._approvals

    def grant_approval(self, approval_id: str) -> None:
        self._approvals.add(approval_id)

    def save_shell_output_as_evidence(
        self,
        task_id: str,
        loop_id: int,
        label: str,
        argv: list[str],
        cwd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> str:
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self._evidence[eid] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "label": label,
            "argv": argv,
            "exit_code": exit_code,
            "type": "shell_output",
        }
        return eid

    def reset_no_progress_streak(self, task_id: str) -> None:
        self._no_progress_streaks[task_id] = 0

    def increment_no_progress_streak(self, task_id: str) -> int:
        current = self._no_progress_streaks.get(task_id, 0)
        self._no_progress_streaks[task_id] = current + 1
        return current + 1

    def next_loop_id(self, task_id: str) -> int:
        current = self._loop_counters.get(task_id, 0)
        self._loop_counters[task_id] = current + 1
        return current + 1

    def append_event(
        self, event_type: str, payload: dict[str, object]
    ) -> None:
        self._events.append({"event_type": event_type, "payload": payload})
