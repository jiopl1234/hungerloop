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
from hungerloop.models.enums import EvidenceType, LoopPhase, StopReason
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    HungerClockState,
    HungerItem,
    HungerLedger,
    HungerPolicy,
    HungerSnapshot,
)
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.planning import LoopPlan
from hungerloop.models.skill import SkillCard
from hungerloop.models.task import TaskRecord
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.usage import UsageSnapshot
from hungerloop.models.validation import ValidationReport
from hungerloop.models.worker import AgentSpec, WorkerResult
from hungerloop.repository.evidence_success import is_successful_evidence_payload


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
        self._loop_plans: dict[tuple[str, int], LoopPlan] = {}

        # Trace / stop
        self._loop_traces: dict[tuple[str, int], LoopTrace] = {}
        self._stop_reports_history: dict[str, list[StopReport]] = {}
        self._usage: dict[str, UsageSnapshot] = {}

        # Memory / skill
        self._memory: dict[str, MemoryCandidate] = {}
        self._skills: dict[str, SkillCard] = {}
        self._committed_refs: dict[str, int] = {}

        # Misc
        self._approvals: set[str] = set()
        self._no_progress_streaks: dict[str, int] = {}
        self._loop_counters: dict[str, int] = {}
        self._events: list[dict[str, object]] = []
        # Task locks: task_id -> {"owner": str, "locked_at": datetime}
        self._task_locks: dict[str, dict[str, Any]] = {}

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

    def task_exists(self, task_id: str) -> bool:
        if task_id in self._tasks:
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

    def append_event(
        self,
        event_type: EventType,
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
        self._events.append(
            {
                "event_type": event_type.value,
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

    def count_committed_references(self, candidate_id: str) -> int:
        return self._committed_refs.get(candidate_id, 0)

    def save_skill_card(self, card: SkillCard) -> None:
        self._skills[card.skill_id] = card

    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]:
        if task_id is None:
            return list(self._skills.values())
        return [s for s in self._skills.values() if s.task_id == task_id]

    # =====================================================================
    # Section 8 — Approvals, misc, transactions
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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """No-op for in-memory; SQLiteRepository wraps BEGIN/COMMIT."""
        yield
