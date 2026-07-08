"""InMemoryRepository — process-local persistence for tests and v0.5a CLI.

Implements :class:`RepositoryProtocol` end-to-end. Designed for unit tests
and the v0.5a Day-3 dummy run; SQLiteRepository (Day 4+) is the production
implementation. ``transaction()`` is a no-op context manager — InMemory
writes are atomic at the dict level.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from hungerloop.models.blackboard import Artifact, BestState, CandidateState
from hungerloop.models.enums import EvidenceType, HungerItemStatus, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.handoff import HandoffProcessingResult
from hungerloop.models.hunger import (
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)
from hungerloop.models.memory import MemoryCandidate, PromotedMemory
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.planning import LoopPlan
from hungerloop.models.refactor import RefactorTransaction, RefactorTransactionStatus
from hungerloop.models.skill import ActiveSkillCard, SkillCard, SkillCardCandidate
from hungerloop.models.task import TaskRecord
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.usage import UsageSnapshot
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.models.worker import AgentSpec, WorkerHandoff, WorkerResult
from hungerloop.repository.evidence_success import is_successful_evidence_payload
from hungerloop.repository.migration_errors import IllegalPhaseTransition


class InMemoryRepository:
    """Dict-backed implementation of :class:`RepositoryProtocol`."""

    def __init__(self) -> None:
        # Hunger
        self._tasks: dict[str, TaskRecord] = {}
        self._policies: dict[str, HungerPolicy] = {}
        self._clocks: dict[str, HungerClockState] = {}
        self._ledgers: dict[str, HungerLedger] = {}
        self._items: dict[str, HungerItem] = {}
        self._snapshots: dict[str, HungerSnapshot] = {}
        self._last_phase: dict[str, LoopPhase] = {}

        # Workspace state
        self._best_states: dict[str, BestState] = {}
        self._candidates: dict[str, CandidateState] = {}
        self._committed: set[str] = set()
        self._rejected: set[str] = set()

        # Validation
        self._validation_reports: dict[str, ValidationReport] = {}
        self._failures: list[ValidationReport] = []
        self._accepted_checks: dict[tuple[str, str], dict[str, Any]] = {}

        # Evidence
        self._evidence: dict[str, dict[str, object]] = {}
        self._artifacts: dict[str, Artifact] = {}

        # Worker / planning
        self._agent_specs: dict[str, AgentSpec] = {}
        self._worker_results: dict[str, WorkerResult] = {}
        self._worker_handoffs: dict[str, WorkerHandoff] = {}
        self._worker_handoff_order: list[str] = []
        self._handoff_processing_results: dict[str, HandoffProcessingResult] = {}
        self._loop_plans: dict[tuple[str, int], LoopPlan] = {}

        # Trace / stop
        self._loop_traces: dict[tuple[str, int], LoopTrace] = {}
        self._stop_reports_history: dict[str, list[StopReport]] = {}
        self._usage: dict[str, UsageSnapshot] = {}

        # Memory / skill
        self._memory: dict[str, MemoryCandidate] = {}
        self._promoted_memories: dict[str, PromotedMemory] = {}
        self._skills: dict[str, SkillCard] = {}
        self._skill_candidates: dict[str, SkillCardCandidate] = {}
        self._active_skills: dict[str, ActiveSkillCard] = {}
        self._committed_refs: dict[str, int] = {}

        # Mission runtime
        self._missions: dict[str, Mission] = {}
        self._mission_ids_by_task: dict[str, str] = {}
        self._mission_phases: dict[str, MissionPhase] = {}
        self._phase_mission_ids: dict[str, str] = {}
        self._mission_features: dict[str, MissionFeature] = {}
        self._feature_mission_ids: dict[str, str] = {}
        self._validation_contract_mission_ids: set[str] = set()
        self._validation_assertions: dict[str, ValidationAssertion] = {}
        self._assertion_mission_ids: dict[str, str] = {}

        # Misc
        self._approvals: set[str] = set()
        self._no_progress_streaks: dict[str, int] = {}
        self._loop_counters: dict[str, int] = {}
        self._events: list[dict[str, object]] = []
        # Task locks: task_id -> {"owner": str, "locked_at": datetime}
        self._task_locks: dict[str, dict[str, Any]] = {}

        # Refactor transactions (v0.7)
        self._refactor_transactions: dict[str, RefactorTransaction] = {}

    # =====================================================================
    # Section 0 — Task metadata
    # =====================================================================
    def create_task(self, task_id: str, raw_goal: str) -> None:
        now = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self._tasks[task_id] = TaskRecord(
            task_id=task_id,
            raw_goal=raw_goal,
            status="pending",
            created_at=now,
            updated_at=now,
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def update_task_status(self, task_id: str, status: str) -> None:
        task = self._tasks.get(task_id)
        now = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        if task is None:
            self._tasks[task_id] = TaskRecord(
                task_id=task_id,
                raw_goal="",
                status=status,
                created_at=now,
                updated_at=now,
            )
            return
        self._tasks[task_id] = task.model_copy(
            update={"status": status, "updated_at": now}
        )

    def list_task_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def task_exists(self, task_id: str) -> bool:
        if task_id in self._tasks:
            return True
        if task_id in self._mission_ids_by_task:
            return True
        if task_id in self._policies or task_id in self._ledgers:
            return True
        if task_id in self._best_states or task_id in self._stop_reports_history:
            return True
        if any(tid == task_id for tid, _ in self._loop_traces):
            return True
        return any(
            isinstance(row, dict) and row.get("task_id") == task_id
            for row in self._events
        )

    # =====================================================================
    # Section 1 — Hunger
    # =====================================================================
    def get_hunger_policy(self, task_id: str) -> HungerPolicy:
        return self._policies.get(task_id, HungerPolicy())

    def set_hunger_policy(self, task_id: str, policy: HungerPolicy) -> None:
        """Test/CLI-only setup helper (not in protocol; per reverse-spec U6)."""
        self._policies[task_id] = policy

    def get_hunger_clock(self, task_id: str) -> HungerClockState:
        if task_id not in self._clocks:
            self._clocks[task_id] = HungerClockState()
        return self._clocks[task_id]

    def save_hunger_clock(self, clock: HungerClockState) -> None:
        # NOTE(reverse-spec U3): in-memory mutates by reference, but we also
        # accept a fresh instance so SQLiteRepository's copy-on-save semantics
        # round-trip through the same protocol shape.
        # Storage key is the clock's owner — we don't have one on the model,
        # so we rely on the existing reference-based contract for in-memory.
        # SQLiteRepository will require ``task_id`` on the clock or on this
        # call; that is a Day-4 concern.
        for tid, existing in self._clocks.items():
            if existing is clock:
                self._clocks[tid] = clock
                return
        # Fall through: clock not previously known; this path is exercised
        # only when callers construct a clock from scratch (rare).

    def get_hunger_ledger(self, task_id: str) -> HungerLedger:
        return self._ledgers.get(task_id, HungerLedger(task_id=task_id, items=[]))

    def save_hunger_ledger(self, task_id: str, ledger: HungerLedger) -> None:
        self._ledgers[task_id] = ledger
        for item in ledger.items:
            self._items[item.id] = item

    # Backward-compat alias used by existing tests.
    set_hunger_ledger = save_hunger_ledger

    def get_hunger_item(self, item_id: str) -> HungerItem | None:
        return self._items.get(item_id)

    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]:
        return [self._items[iid] for iid in item_ids if iid in self._items]

    def save_hunger_item(self, item: HungerItem) -> None:
        self._items[item.id] = item

    def update_hunger_item_status(
        self,
        task_id: str,
        item_id: str,
        status: HungerItemStatus | str,
    ) -> None:
        current = self._items.get(item_id)
        if current is None:
            raise KeyError(f"Unknown hunger item: {item_id}")
        normalized = (
            status if isinstance(status, HungerItemStatus) else HungerItemStatus(status)
        )
        updated = current.model_copy(update={"status": normalized})
        self._items[item_id] = updated
        ledger = self.get_hunger_ledger(task_id)
        self._ledgers[task_id] = HungerLedger(
            task_id=task_id,
            items=[
                updated if existing.id == item_id else existing
                for existing in ledger.items
            ],
        )

    def get_items_for_check_keys(
        self, task_id: str, check_keys: list[str]
    ) -> list[HungerItem]:
        item_ids = {k.split(":", 1)[0] for k in check_keys}
        return [self._items[iid] for iid in item_ids if iid in self._items]

    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None:
        self._snapshots[task_id] = snapshot
        self._last_phase[task_id] = snapshot.phase

    def get_last_phase(self, task_id: str) -> LoopPhase | None:
        return self._last_phase.get(task_id)

    def get_latest_hunger_snapshot(self, task_id: str) -> HungerSnapshot | None:
        return self._snapshots.get(task_id)

    # =====================================================================
    # Section 2 — Workspace state
    # =====================================================================
    def get_best_state(self, task_id: str) -> BestState | None:
        return self._best_states.get(task_id)

    def save_best_state(self, best: BestState) -> None:
        self._best_states[best.task_id] = best
        self._committed_refs[best.state_id] = (
            self._committed_refs.get(best.state_id, 0) + 1
        )

    def save_candidate(self, candidate: CandidateState) -> None:
        self._candidates[candidate.id] = candidate

    def mark_candidate_committed(self, candidate_id: str) -> None:
        self._committed.add(candidate_id)

    def mark_candidate_rejected(self, candidate_id: str) -> None:
        self._rejected.add(candidate_id)

    def list_candidates_for_task(self, task_id: str) -> list[CandidateState]:
        return sorted(
            (c for c in self._candidates.values() if c.task_id == task_id),
            key=lambda c: c.loop_id,
        )

    def get_candidate(self, candidate_id: str) -> CandidateState | None:
        return self._candidates.get(candidate_id)

    def candidate_status(
        self, candidate_id: str
    ) -> Literal["pending", "committed", "rejected", "missing"]:
        if candidate_id not in self._candidates:
            return "missing"
        if candidate_id in self._committed:
            return "committed"
        if candidate_id in self._rejected:
            return "rejected"
        return "pending"

    # =====================================================================
    # Section 3 — Validation
    # =====================================================================
    def save_validation_report(self, report: ValidationReport) -> None:
        self._validation_reports[report.id] = report

    def add_failure_from_validation(self, report: ValidationReport) -> None:
        self._failures.append(report)

    def get_validation_report(
        self, validation_id: str
    ) -> ValidationReport | None:
        return self._validation_reports.get(validation_id)

    def validation_exists(self, validation_id: str) -> bool:
        return validation_id in self._validation_reports

    def iter_accepted_checks(self, task_id: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for (rec_task, check_key), record in sorted(self._accepted_checks.items()):
            if rec_task != task_id:
                continue
            row: dict[str, object] = {"check_key": check_key}
            row.update(record)
            rows.append(row)
        return rows

    def save_accepted_check(
        self,
        *,
        task_id: str,
        check_key: str,
        hunger_item_id: str,
        check_index: int,
        accepted_at_loop: int,
        validation_id: str,
        evidence_id: str | None,
    ) -> None:
        self._accepted_checks[(task_id, check_key)] = {
            "hunger_item_id": hunger_item_id,
            "check_index": check_index,
            "accepted_at_loop": accepted_at_loop,
            "validation_id": validation_id,
            "evidence_id": evidence_id,
        }

    # =====================================================================
    # Section 4 — Evidence
    # =====================================================================
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
            "type": EvidenceType.SANDBOX_RUN.value,
            "timed_out": timed_out,
        }
        return eid

    def save_model_call_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        response_preview: str,
    ) -> str:
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self._evidence[eid] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "response_preview": response_preview,
            "type": EvidenceType.MODEL_CALL.value,
        }
        # Bump usage counters (the model client also records via CostGuard,
        # but we mirror llm_calls here so get_usage_snapshot is meaningful
        # for the in-memory path).
        usage = self._usage.setdefault(task_id, UsageSnapshot(task_id=task_id))
        usage.llm_calls += 1
        usage.tokens += input_tokens + output_tokens
        usage.cost_usd += cost_usd
        return eid

    def save_model_error_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        agent_id: str,
        provider: str,
        model: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> str:
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self._evidence[eid] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "provider": provider,
            "model": model,
            "error_type": error_type,
            "error_message": error_message,
            "retryable": retryable,
            "type": EvidenceType.MODEL_ERROR.value,
        }
        return eid

    def save_tool_call_as_evidence(
        self,
        *,
        task_id: str,
        loop_id: int,
        agent_id: str,
        tool_name: str,
        args_summary: str,
        result_summary: str,
        success: bool,
        elapsed_ms: int,
    ) -> str:
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self._evidence[eid] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "args_summary": args_summary,
            "result_summary": result_summary,
            "success": success,
            "elapsed_ms": elapsed_ms,
            "type": EvidenceType.TOOL_CALL.value,
        }
        usage = self._usage.setdefault(task_id, UsageSnapshot(task_id=task_id))
        usage.tool_calls += 1
        return eid

    def save_evidence(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        evidence_type: EvidenceType | str,
        payload: dict[str, object],
    ) -> str:
        actual_type = (
            evidence_type.value
            if isinstance(evidence_type, EvidenceType)
            else evidence_type
        )
        eid = f"ev-{uuid.uuid4().hex[:8]}"
        self._evidence[eid] = {
            **payload,
            "task_id": task_id,
            "loop_id": loop_id,
            "type": actual_type,
        }
        return eid

    def list_evidence(
        self,
        task_id: str,
        *,
        evidence_type: EvidenceType | str | None = None,
    ) -> list[dict[str, object]]:
        wanted = (
            evidence_type.value
            if isinstance(evidence_type, EvidenceType)
            else evidence_type
        )
        out: list[dict[str, object]] = []
        for evidence_id, evidence in self._evidence.items():
            if evidence.get("task_id") != task_id:
                continue
            if wanted is not None and evidence.get("type") != wanted:
                continue
            out.append({"evidence_id": evidence_id, **evidence})
        return out

    def count_evidence_by_type(
        self,
        task_id: str,
        evidence_ids: list[str],
        evidence_type: EvidenceType | str,
        *,
        successful_only: bool = False,
    ) -> int:
        wanted = (
            evidence_type.value
            if isinstance(evidence_type, EvidenceType)
            else evidence_type
        )
        return sum(
            1
            for eid in evidence_ids
            if self._evidence_matches(
                task_id=task_id,
                evidence_id=eid,
                evidence_type=wanted,
                successful_only=successful_only,
            )
        )

    def _evidence_matches(
        self,
        *,
        task_id: str,
        evidence_id: str,
        evidence_type: str,
        successful_only: bool,
    ) -> bool:
        evidence = self._evidence.get(evidence_id)
        if evidence is None or evidence.get("task_id") != task_id:
            return False
        actual_type = str(evidence.get("type", ""))
        if evidence_type != "any" and actual_type != evidence_type:
            return False
        if not successful_only:
            return True
        return is_successful_evidence_payload(actual_type, evidence)

    def list_successful_tool_call_evidence(
        self, task_id: str
    ) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for eid, row in self._evidence.items():
            if row.get("task_id") != task_id:
                continue
            if row.get("type") != EvidenceType.TOOL_CALL.value:
                continue
            if row.get("success") is not True:
                continue
            out.append(
                {
                    "evidence_id": eid,
                    "task_id": task_id,
                    "loop_id": row.get("loop_id"),
                    "payload": dict(row),
                }
            )
        return out

    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[Artifact]:
        return [self._artifacts[aid] for aid in artifact_ids if aid in self._artifacts]

    def save_artifact(self, artifact: Artifact) -> None:
        self._artifacts[artifact.artifact_id] = artifact

    # =====================================================================
    # Section 5 — Worker / Planning
    # =====================================================================
    def get_agent_spec(self, agent_id: str) -> AgentSpec:
        if agent_id not in self._agent_specs:
            raise KeyError(f"AgentSpec not registered: {agent_id}")
        return self._agent_specs[agent_id]

    def save_agent_spec(self, spec: AgentSpec) -> None:
        self._agent_specs[spec.agent_id] = spec

    def save_worker_result(self, result: WorkerResult) -> None:
        rid = f"WR-{result.task_id}-{result.loop_id}-{result.agent_id}"
        self._worker_results[rid] = result

    def save_worker_handoff(self, handoff: WorkerHandoff) -> str:
        handoff_id = handoff.handoff_id or f"WH-{uuid.uuid4()}"
        self._worker_handoffs[handoff_id] = handoff
        self._worker_handoff_order.append(handoff_id)
        return handoff_id

    def save_handoff_processing_result(
        self,
        task_id: str,
        result: HandoffProcessingResult,
    ) -> None:
        self._handoff_processing_results[task_id] = result

    def list_worker_handoffs(
        self,
        task_id: str,
        *,
        since_loop_id: int | None = None,
        limit: int | None = None,
    ) -> list[WorkerHandoff]:
        matches = [
            (index, self._worker_handoffs[handoff_id])
            for index, handoff_id in enumerate(self._worker_handoff_order)
            if self._worker_handoffs[handoff_id].task_id == task_id
            and (
                since_loop_id is None
                or self._worker_handoffs[handoff_id].loop_id >= since_loop_id
            )
        ]
        matches.sort(key=lambda item: (item[1].loop_id, item[0]))
        handoffs = [handoff for _index, handoff in matches]
        if limit is not None:
            return handoffs[:limit]
        return handoffs

    def get_last_worker_handoff(
        self,
        task_id: str,
        agent_id: str,
        *,
        before_loop_id: int,
    ) -> WorkerHandoff | None:
        matches = [
            (index, self._worker_handoffs[handoff_id])
            for index, handoff_id in enumerate(self._worker_handoff_order)
            if self._worker_handoffs[handoff_id].task_id == task_id
            and self._worker_handoffs[handoff_id].agent_id == agent_id
            and self._worker_handoffs[handoff_id].loop_id < before_loop_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item[1].loop_id, item[0]))[1]

    def get_last_worker_result(
        self,
        task_id: str,
        agent_id: str,
        before_loop_id: int,
    ) -> WorkerResult | None:
        handoff = self.get_last_worker_handoff(
            task_id,
            agent_id,
            before_loop_id=before_loop_id,
        )
        if handoff is not None:
            return handoff.as_worker_result()

        matches = [
            result
            for result in self._worker_results.values()
            if result.task_id == task_id
            and result.agent_id == agent_id
            and result.loop_id < before_loop_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda result: result.loop_id)

    def get_latest_handoff_processing_result(
        self,
        task_id: str,
    ) -> HandoffProcessingResult | None:
        return self._handoff_processing_results.get(task_id)

    def save_loop_plan(self, plan: LoopPlan) -> None:
        self._loop_plans[(plan.task_id, plan.loop_id)] = plan

    # =====================================================================
    # Section 6 — Trace / Stop
    # =====================================================================
    def save_loop_trace(self, trace: LoopTrace) -> None:
        self._loop_traces[(trace.task_id, trace.loop_id)] = trace

    def list_loop_traces(self, task_id: str) -> list[LoopTrace]:
        return [
            trace
            for (tid, _loop_id), trace in sorted(self._loop_traces.items())
            if tid == task_id
        ]

    def get_loop_trace(self, task_id: str, loop_id: int) -> LoopTrace | None:
        return self._loop_traces.get((task_id, loop_id))

    def save_stop_report(self, report: StopReport) -> None:
        self._stop_reports_history.setdefault(report.task_id, []).append(report)
        task = self._tasks.get(report.task_id)
        if task is not None:
            now = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            self._tasks[report.task_id] = task.model_copy(
                update={
                    "status": "stopped",
                    "last_stop_reason": report.stop_reason,
                    "updated_at": now,
                }
            )

    def get_last_stop_report(self, task_id: str) -> StopReport | None:
        history = self._stop_reports_history.get(task_id)
        return history[-1] if history else None

    def get_last_stop_reason(self, task_id: str) -> StopReason | None:
        report = self.get_last_stop_report(task_id)
        return report.stop_reason if report else None

    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot:
        return self._usage.setdefault(task_id, UsageSnapshot(task_id=task_id))

    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> None:
        self._usage[snapshot.task_id] = snapshot.model_copy()

    def aggregate_evidence_usage(self, task_id: str) -> UsageSnapshot:
        tokens = 0
        cost = 0.0
        llm = 0
        tool = 0
        for row in self._evidence.values():
            if row.get("task_id") != task_id:
                continue
            ev_type = row.get("type")
            if ev_type == EvidenceType.MODEL_CALL.value:
                input_tokens = row.get("input_tokens", 0)
                output_tokens = row.get("output_tokens", 0)
                cost_usd = row.get("cost_usd", 0.0)
                if isinstance(input_tokens, int):
                    tokens += input_tokens
                if isinstance(output_tokens, int):
                    tokens += output_tokens
                if isinstance(cost_usd, (int, float)):
                    cost += float(cost_usd)
                llm += 1
            elif ev_type == EvidenceType.TOOL_CALL.value:
                tool += 1
        return UsageSnapshot(
            task_id=task_id,
            tokens=tokens,
            cost_usd=cost,
            llm_calls=llm,
            tool_calls=tool,
        )

    def append_event(
        self,
        event_type: EventType | str,
        payload: dict[str, object],
        *,
        task_id: str | None = None,
        loop_id: int | None = None,
    ) -> None:
        # Store the string ``.value`` so existing tests asserting on
        # ``event_type == "hunger_resumed"`` keep working and SQLiteRepo's
        # eventual TEXT column stays plain. ``created_at`` mirrors the v1
        # ``events.created_at`` column shape (ISO8601 UTC with the 'Z'
        # suffix) so ``hungerloop trace export`` can stream rows directly.
        actual_event_type = (
            event_type.value if isinstance(event_type, EventType) else event_type
        )
        self._events.append(
            {
                "event_type": actual_event_type,
                "payload": payload,
                "task_id": task_id,
                "loop_id": loop_id,
                "created_at": (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
        )

    def list_events(
        self,
        task_id: str,
        *,
        since_loop: int | None = None,
        until_loop: int | None = None,
        event_types: list[str] | None = None,
        include_global: bool = False,
    ) -> list[dict[str, object]]:
        wanted_types: set[str] | None = (
            set(event_types) if event_types is not None else None
        )
        out: list[dict[str, object]] = []
        for row in self._events:
            if not isinstance(row, dict):
                continue
            row_task = row.get("task_id")
            if row_task == task_id:
                pass
            elif row_task is None and include_global:
                pass
            else:
                continue
            row_loop = row.get("loop_id")
            if since_loop is not None:
                if not isinstance(row_loop, int) or row_loop < since_loop:
                    continue
            if until_loop is not None:
                if not isinstance(row_loop, int) or row_loop > until_loop:
                    continue
            if wanted_types is not None and row.get("event_type") not in wanted_types:
                continue
            out.append(row)
        return out

    # =====================================================================
    # Section 7 — Memory / Skill
    # =====================================================================
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None:
        self._memory[candidate.candidate_id] = candidate

    def list_memory_candidates(self, task_id: str) -> list[MemoryCandidate]:
        return [c for c in self._memory.values() if c.task_id == task_id]

    def get_memory_candidate(
        self, candidate_id: str
    ) -> MemoryCandidate | None:
        return self._memory.get(candidate_id)

    def count_committed_references(self, candidate_id: str) -> int:
        return self._committed_refs.get(candidate_id, 0)

    def save_promoted_memory(self, memory: PromotedMemory) -> None:
        self._promoted_memories[memory.memory_id] = memory

    def list_promoted_memories(
        self, task_id: str | None = None
    ) -> list[PromotedMemory]:
        if task_id is None:
            return list(self._promoted_memories.values())
        return [
            m for m in self._promoted_memories.values() if m.task_id == task_id
        ]

    def get_promoted_memory(self, memory_id: str) -> PromotedMemory | None:
        return self._promoted_memories.get(memory_id)

    def save_skill_card(self, card: SkillCard) -> None:
        self._skills[card.skill_id] = card

    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]:
        if task_id is None:
            return list(self._skills.values())
        return [s for s in self._skills.values() if s.task_id == task_id]

    def save_skill_card_candidate(
        self, candidate: SkillCardCandidate
    ) -> None:
        self._skill_candidates[candidate.skill_candidate_id] = candidate

    def get_skill_card_candidate(
        self, skill_candidate_id: str
    ) -> SkillCardCandidate | None:
        return self._skill_candidates.get(skill_candidate_id)

    def list_skill_card_candidates(
        self,
        *,
        task_id: str | None = None,
        state: str | None = None,
    ) -> list[SkillCardCandidate]:
        out = list(self._skill_candidates.values())
        if task_id is not None:
            out = [c for c in out if c.task_id == task_id]
        if state is not None and state != "all":
            out = [c for c in out if c.state == state]
        return out

    def save_active_skill_card(self, skill: ActiveSkillCard) -> None:
        self._active_skills[skill.skill_id] = skill

    def get_active_skill_card(self, skill_id: str) -> ActiveSkillCard | None:
        return self._active_skills.get(skill_id)

    def list_active_skill_cards(
        self, *, state: str | None = None
    ) -> list[ActiveSkillCard]:
        out = list(self._active_skills.values())
        if state is not None and state != "all":
            out = [s for s in out if s.state == state]
        return out

    # =====================================================================
    # Section 8 — Mission runtime
    # =====================================================================
    def save_mission(self, mission: Mission) -> None:
        previous = self._missions.get(mission.mission_id)
        if previous is not None and previous.task_id != mission.task_id:
            self._mission_ids_by_task.pop(previous.task_id, None)

        self._missions[mission.mission_id] = mission.model_copy(deep=True)
        self._mission_ids_by_task[mission.task_id] = mission.mission_id

        phase_ids = {
            phase_id
            for phase_id, parent_mission_id in self._phase_mission_ids.items()
            if parent_mission_id == mission.mission_id
        }
        for phase_id in phase_ids:
            self._phase_mission_ids.pop(phase_id, None)
            self._mission_phases.pop(phase_id, None)

        feature_ids = {
            feature_id
            for feature_id, parent_mission_id in self._feature_mission_ids.items()
            if parent_mission_id == mission.mission_id
        }
        for feature_id in feature_ids:
            self._feature_mission_ids.pop(feature_id, None)
            self._mission_features.pop(feature_id, None)

        for phase in mission.phases:
            self._phase_mission_ids[phase.phase_id] = mission.mission_id
            self._mission_phases[phase.phase_id] = phase.model_copy(deep=True)
        for feature in mission.features:
            self._feature_mission_ids[feature.feature_id] = mission.mission_id
            self._mission_features[feature.feature_id] = feature.model_copy(deep=True)

    def get_mission(self, task_id: str) -> Mission | None:
        mission_id = self._mission_ids_by_task.get(task_id)
        if mission_id is None:
            return None
        mission = self._missions.get(mission_id)
        if mission is None:
            return None
        return mission.model_copy(
            update={
                "phases": self.list_mission_phases(mission_id),
                "features": self.list_mission_features(mission_id=mission_id),
            },
            deep=True,
        )

    def save_mission_phase(self, phase: MissionPhase) -> None:
        mission_id = self._phase_mission_ids.get(phase.phase_id)
        if mission_id is None:
            raise KeyError(f"Unknown mission phase: {phase.phase_id}")
        self._phase_mission_ids[phase.phase_id] = mission_id
        self._mission_phases[phase.phase_id] = phase.model_copy(deep=True)

    def update_phase_status(
        self,
        phase_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        phase = self._mission_phases[phase_id]
        if phase.status == "done" and status != "done":
            raise IllegalPhaseTransition(
                f"Mission phase {phase_id!r} cannot transition from done to {status!r}",
                phase_id=phase_id,
                from_status=phase.status,
                to_status=status,
            )
        self._mission_phases[phase_id] = phase.model_copy(
            update={"status": status, "completed_at": completed_at}
        )

    def list_mission_phases(self, mission_id: str) -> list[MissionPhase]:
        return [
            phase
            for phase in self._mission_phases.values()
            if self._phase_mission_ids.get(phase.phase_id) == mission_id
        ]

    def save_mission_feature(self, feature: MissionFeature) -> None:
        mission_id = self._feature_mission_ids.get(feature.feature_id)
        if mission_id is None:
            mission_id = self._phase_mission_ids.get(feature.phase_id)
        if mission_id is None:
            raise KeyError(f"Unknown mission feature phase: {feature.phase_id}")
        self._feature_mission_ids[feature.feature_id] = mission_id
        self._mission_features[feature.feature_id] = feature.model_copy(deep=True)

    def update_feature_status(
        self,
        feature_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        del completed_at
        feature = self._mission_features[feature_id]
        if feature.status == "blocked" and status == "done":
            raise IllegalPhaseTransition(
                f"Mission feature {feature_id!r} cannot transition from blocked to done",
                feature_id=feature_id,
                from_status=feature.status,
                to_status=status,
            )
        self._mission_features[feature_id] = feature.model_copy(
            update={"status": status}
        )

    def list_mission_features(
        self,
        mission_id: str | None = None,
        phase_id: str | None = None,
    ) -> list[MissionFeature]:
        features = list(self._mission_features.values())
        if mission_id is not None:
            features = [
                feature
                for feature in features
                if self._feature_mission_ids.get(feature.feature_id) == mission_id
            ]
        if phase_id is not None:
            features = [feature for feature in features if feature.phase_id == phase_id]
        return features

    def list_features_for_phase(self, phase_id: str) -> list[MissionFeature]:
        return self.list_mission_features(phase_id=phase_id)

    def save_validation_contract(self, contract: ValidationContract) -> None:
        self._validation_contract_mission_ids.add(contract.mission_id)

        assertion_ids = {
            assertion_id
            for assertion_id, parent_mission_id in self._assertion_mission_ids.items()
            if parent_mission_id == contract.mission_id
        }
        for assertion_id in assertion_ids:
            self._assertion_mission_ids.pop(assertion_id, None)
            self._validation_assertions.pop(assertion_id, None)

        for assertion in contract.assertions:
            self._assertion_mission_ids[assertion.assertion_id] = contract.mission_id
            self._validation_assertions[assertion.assertion_id] = assertion.model_copy(
                deep=True
            )

    def get_validation_contract(self, mission_id: str) -> ValidationContract | None:
        if mission_id not in self._validation_contract_mission_ids and all(
            parent_mission_id != mission_id
            for parent_mission_id in self._assertion_mission_ids.values()
        ):
            return None
        return ValidationContract(
            mission_id=mission_id,
            assertions=self.list_validation_assertions(mission_id=mission_id),
        )

    def save_validation_assertion(self, assertion: ValidationAssertion) -> None:
        mission_id = self._phase_mission_ids.get(assertion.phase_id)
        if mission_id is None:
            raise KeyError(f"Unknown mission phase for assertion: {assertion.phase_id}")
        self._validation_contract_mission_ids.add(mission_id)
        self._assertion_mission_ids[assertion.assertion_id] = mission_id
        self._validation_assertions[assertion.assertion_id] = assertion.model_copy(
            deep=True
        )

    def update_assertion_status(
        self,
        assertion_id: str,
        status: str,
        *,
        validated_at_loop: int | None = None,
        evidence_ids: list[str] | None = None,
    ) -> None:
        assertion = self._validation_assertions[assertion_id]
        self._validation_assertions[assertion_id] = assertion.model_copy(
            update={
                "status": status,
                "validated_at_loop": (
                    validated_at_loop
                    if validated_at_loop is not None
                    else assertion.validated_at_loop
                ),
                "evidence_ids": (
                    list(evidence_ids)
                    if evidence_ids is not None
                    else list(assertion.evidence_ids)
                ),
            }
        )

    def list_validation_assertions(
        self,
        mission_id: str | None = None,
        phase_id: str | None = None,
    ) -> list[ValidationAssertion]:
        assertions = list(self._validation_assertions.values())
        if mission_id is not None:
            assertions = [
                assertion
                for assertion in assertions
                if self._assertion_mission_ids.get(assertion.assertion_id) == mission_id
            ]
        if phase_id is not None:
            assertions = [
                assertion for assertion in assertions if assertion.phase_id == phase_id
            ]
        return assertions

    def count_validation_contract_summary(self, mission_id: str) -> dict[str, int]:
        counts = {
            "pending": 0,
            "passed": 0,
            "failed": 0,
            "blocked": 0,
        }
        for assertion in self.list_validation_assertions(mission_id=mission_id):
            counts[assertion.status] += 1
        return counts

    # =====================================================================
    # Section 9 — Approvals, misc, transactions
    # =====================================================================
    def is_approval_granted(self, approval_id: str) -> bool:
        return approval_id in self._approvals

    def grant_approval(self, approval_id: str) -> None:
        """Test helper (not in protocol)."""
        self._approvals.add(approval_id)

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

    # ---- Task lock (PRD §5.1.1) ------------------------------------------
    @staticmethod
    def _same_host_pid(owner_a: str, owner_b: str) -> bool:
        """Compare ``hostname:pid`` prefixes; the ``:uuid`` tail differs
        between calls but the same physical process keeps the same prefix."""
        head_a = owner_a.rsplit(":", 1)[0]
        head_b = owner_b.rsplit(":", 1)[0]
        return head_a == head_b

    def acquire_task_lock(
        self,
        task_id: str,
        owner: str,
        *,
        stale_threshold_seconds: int,
        steal: bool = False,
    ) -> Literal["acquired", "reentrant", "held_live", "held_stale", "stolen"]:
        now = datetime.now(timezone.utc)
        existing = self._task_locks.get(task_id)

        if existing is None:
            self._task_locks[task_id] = {"owner": owner, "locked_at": now}
            return "acquired"

        if self._same_host_pid(existing["owner"], owner):
            # Re-entrant from the same physical process — no mutation.
            return "reentrant"

        elapsed = (now - existing["locked_at"]).total_seconds()
        is_stale = elapsed >= stale_threshold_seconds

        if not is_stale and not steal:
            return "held_live"

        if is_stale and not steal:
            return "held_stale"

        # steal=True path — replace the owner and audit the takeover.
        prev_owner = existing["owner"]
        prev_locked_at = existing["locked_at"]
        self._task_locks[task_id] = {"owner": owner, "locked_at": now}
        # Local import to keep the v0.5a module surface unchanged.
        from hungerloop.models.events import EventType

        self.append_event(
            EventType.LOCK_STOLEN,
            {
                "prev_owner": prev_owner,
                # Match the events.created_at Z-suffix shape so
                # ``trace export`` sees consistent timestamps across
                # the wire payload and the row-level field.
                "prev_locked_at": (
                    prev_locked_at.isoformat().replace("+00:00", "Z")
                ),
                "new_owner": owner,
            },
            task_id=task_id,
        )
        return "stolen"

    def release_task_lock(self, task_id: str, owner: str) -> None:
        existing = self._task_locks.get(task_id)
        if existing is None:
            return
        # Only release if the caller still owns the lock — defends against
        # double-release after a steal.
        if existing["owner"] == owner:
            del self._task_locks[task_id]

    def get_task_lock(self, task_id: str) -> dict[str, object] | None:
        info = self._task_locks.get(task_id)
        if info is None:
            return None
        # Return a shallow copy so callers can't mutate stored state.
        return dict(info)

    # =====================================================================
    # Section 10 — Refactor transactions (v0.7)
    # =====================================================================
    def save_refactor_transaction(self, txn: RefactorTransaction) -> None:
        # Single-open enforcement: reject saving a second open transaction
        # for the same task.
        if txn.status == RefactorTransactionStatus.OPEN:
            for existing in self._refactor_transactions.values():
                if (
                    existing.task_id == txn.task_id
                    and existing.status == RefactorTransactionStatus.OPEN
                    and existing.transaction_id != txn.transaction_id
                ):
                    raise ValueError(
                        f"An open refactor transaction already exists for task "
                        f"{txn.task_id}: {existing.transaction_id}"
                    )
        self._refactor_transactions[txn.transaction_id] = txn.model_copy(deep=True)

    def get_refactor_transaction(
        self, transaction_id: str
    ) -> RefactorTransaction | None:
        txn = self._refactor_transactions.get(transaction_id)
        return txn.model_copy(deep=True) if txn is not None else None

    def get_open_refactor_transaction(
        self, task_id: str
    ) -> RefactorTransaction | None:
        for txn in self._refactor_transactions.values():
            if (
                txn.task_id == task_id
                and txn.status == RefactorTransactionStatus.OPEN
            ):
                return txn.model_copy(deep=True)
        return None

    def list_refactor_transactions(
        self, task_id: str
    ) -> list[RefactorTransaction]:
        return sorted(
            (
                txn.model_copy(deep=True)
                for txn in self._refactor_transactions.values()
                if txn.task_id == task_id
            ),
            key=lambda t: t.opening_loop,
        )

    def update_refactor_transaction_status(
        self,
        *,
        transaction_id: str,
        status: RefactorTransactionStatus,
        closed_loop: int | None = None,
        close_reason: str | None = None,
    ) -> RefactorTransaction | None:
        # Validate status: accept RefactorTransactionStatus enum or a valid
        # string value, reject unknown strings.
        if isinstance(status, str) and not isinstance(status, RefactorTransactionStatus):
            try:
                status = RefactorTransactionStatus(status)
            except ValueError:
                raise ValueError(f"Invalid refactor transaction status: {status}")
        txn = self._refactor_transactions.get(transaction_id)
        if txn is None:
            return None
        updated = txn.model_copy(
            update={
                "status": status,
                "closed_loop": closed_loop,
                "close_reason": close_reason,
            }
        )
        self._refactor_transactions[transaction_id] = updated
        return updated.model_copy(deep=True)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """No-op for in-memory; SQLiteRepository wraps BEGIN/COMMIT."""
        yield
