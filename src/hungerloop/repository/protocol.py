"""RepositoryProtocol for HungerLoop v0.5a.

One fat protocol (ADR-006) organized in eight sections. Every new method
added in v0.5a is documented inline. :class:`~hungerloop.repository.
in_memory_repo.InMemoryRepository` implements this protocol for tests;
``SQLiteRepository`` (Day 3+) will be the production implementation.

The ``transaction()`` context manager is mandatory (ADR-001); cross-cutting
writes such as ``CommitManager.apply`` execute inside it.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol

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


class RepositoryProtocol(Protocol):
    """Single persistence protocol for all HungerLoop services (ADR-006)."""

    # =====================================================================
    # Section 0 — Task metadata
    # =====================================================================
    def create_task(self, task_id: str, raw_goal: str) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def task_exists(self, task_id: str) -> bool: ...
    def set_hunger_policy(self, task_id: str, policy: HungerPolicy) -> None: ...

    # =====================================================================
    # Section 1 — Hunger (policy / clock / ledger / items / snapshots)
    # =====================================================================
    def get_hunger_policy(self, task_id: str) -> HungerPolicy: ...
    def get_hunger_clock(self, task_id: str) -> HungerClockState: ...
    def save_hunger_clock(self, clock: HungerClockState) -> None: ...

    def get_hunger_ledger(self, task_id: str) -> HungerLedger: ...
    def save_hunger_ledger(self, task_id: str, ledger: HungerLedger) -> None: ...
    """New in v0.5a: explicit ledger write so SQLiteRepository can persist
    compiler output (reverse-spec U9)."""

    def get_hunger_item(self, item_id: str) -> HungerItem | None: ...
    def get_hunger_items(self, item_ids: list[str]) -> list[HungerItem]: ...
    def save_hunger_item(self, item: HungerItem) -> None: ...
    def get_items_for_check_keys(
        self, task_id: str, check_keys: list[str]
    ) -> list[HungerItem]: ...

    def save_hunger_snapshot(self, task_id: str, snapshot: HungerSnapshot) -> None: ...
    def get_last_phase(self, task_id: str) -> LoopPhase | None: ...
    def get_latest_hunger_snapshot(self, task_id: str) -> HungerSnapshot | None: ...

    # =====================================================================
    # Section 2 — Workspace state (best / candidates)
    # =====================================================================
    def get_best_state(self, task_id: str) -> BestState | None: ...
    def save_best_state(self, best: BestState) -> None: ...

    def save_candidate(self, candidate: CandidateState) -> None: ...
    def mark_candidate_committed(self, candidate_id: str) -> None: ...
    def mark_candidate_rejected(self, candidate_id: str) -> None: ...

    # =====================================================================
    # Section 3 — Validation
    # =====================================================================
    def save_validation_report(self, report: ValidationReport) -> None: ...
    def add_failure_from_validation(self, report: ValidationReport) -> None: ...

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
    ) -> None: ...
    """New in v0.5a (§28.9 / M9): per-check accepted record. Populated by
    ``CommitManager`` on promote; enables MemoryManager's action_verified
    predicate to query without deserialising BestState.payload."""

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
    ) -> str: ...

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
    ) -> str: ...
    """New in v0.5a (§11.2 / §16.3): successful LLM call evidence."""

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
    ) -> str: ...
    """New in v0.5a (§12.3 / §28.17 / M8): ``loop_id`` nullable so
    ModelConfigLoader failures can still be recorded."""

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
    ) -> str: ...
    """New in v0.5a (§28.5 / M18): ToolHarness writes one row per tool call."""

    def count_evidence_by_type(
        self,
        task_id: str,
        evidence_ids: list[str],
        evidence_type: EvidenceType | str,
        *,
        successful_only: bool = False,
    ) -> int: ...
    """Accepts ``EvidenceType`` enum or raw string for backward compatibility
    (``"any"`` still means "don't filter by type"). ``successful_only`` counts
    only evidence rows that prove a successful action or call."""

    def get_artifacts_by_ids(self, artifact_ids: list[str]) -> list[Artifact]: ...

    def save_artifact(self, artifact: Artifact) -> None: ...
    """New in v0.5a (Day 5): ToolHarness persists artifacts produced by
    write_file / patch_file. Previously a test-only helper on
    InMemoryRepository; promoted to the protocol now that the production
    path needs it."""

    # =====================================================================
    # Section 5 — Worker / Planning
    # =====================================================================
    def get_agent_spec(self, agent_id: str) -> AgentSpec: ...
    """New in v0.5a (§28 / M2): resolves to AgentSpecRegistry in v0.5a;
    backed by the agent_specs table from v0.6+."""

    def save_agent_spec(self, spec: AgentSpec) -> None: ...
    """New in v0.5a: forward-compat; v0.5a never writes (registry is
    code-only per ADR-001 compliance)."""

    def save_worker_result(self, result: WorkerResult) -> None: ...
    """New in v0.5a (§28 / N2)."""

    def save_loop_plan(self, plan: LoopPlan) -> None: ...
    """New in v0.5a (§28 / N2)."""

    # =====================================================================
    # Section 6 — Trace / Stop
    # =====================================================================
    def save_loop_trace(self, trace: LoopTrace) -> None: ...

    def list_loop_traces(self, task_id: str) -> list[LoopTrace]: ...
    """v0.5b.0 (PRD §4.1): chronological list of LoopTrace rows for a
    task; used by ``hungerloop report`` to surface the last loop's
    delta_summary and by ``hungerloop trace export`` (v0.5c.1)."""
    """New in v0.5a (§28 / N2)."""

    def save_stop_report(self, report: StopReport) -> None: ...
    """New in v0.5a (§28.15): implementations keep a history list."""

    def get_last_stop_report(self, task_id: str) -> StopReport | None: ...
    def get_last_stop_reason(self, task_id: str) -> StopReason | None: ...
    """New in v0.5a (§18.3): CLI resume preflight calls this before
    invoking the Orchestrator."""

    def get_usage_snapshot(self, task_id: str) -> UsageSnapshot: ...
    """New in v0.5a (§16.4): cumulative tokens / cost / llm / tool call
    counters. Read by Orchestrator before and after each loop to compute
    per-loop deltas for LoopTrace."""

    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> None: ...
    """New in v0.5d.0 (D0-08 / PRD §8.7): explicit upsert path so callers
    can persist a recomputed snapshot atomically (e.g. BudgetGuard's
    per-loop reconciliation, tests, and CLI repair flows). The shipped
    evidence-write paths (``save_model_call_as_evidence`` and
    ``save_tool_call_as_evidence``) continue to bump usage as a
    side-effect; this method exists for callers that already hold a
    fully-formed :class:`UsageSnapshot`."""

    def append_event(
        self,
        event_type: EventType,
        payload: dict[str, object],
        *,
        task_id: str | None = None,
        loop_id: int | None = None,
    ) -> None: ...
    """v0.5a (§28.14 / M15): ``task_id`` and ``loop_id`` are kwargs; pass
    ``None`` for global events.

    v0.5b (PRD §22.8): ``event_type`` is an :class:`EventType` enum
    member. Implementations store ``event_type.value`` so the SQL column
    stays plain TEXT. Adding new enum members is additive; renaming or
    removing requires a coordinated schema migration."""

    def list_events(self, task_id: str) -> list[dict[str, object]]: ...
    """Per-task event rows in append order. Global events are intentionally
    excluded from this query so ``trace export <task_id>`` remains a
    strict per-task projection."""

    # =====================================================================
    # Section 7 — Memory / Skill
    # =====================================================================
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None: ...
    def list_memory_candidates(self, task_id: str) -> list[MemoryCandidate]: ...
    def count_committed_references(self, candidate_id: str) -> int: ...
    """Used by ``MemoryManager.non_volatile`` predicate (§19.2)."""

    def save_skill_card(self, card: SkillCard) -> None: ...
    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]: ...

    # =====================================================================
    # Section 8 — Approvals, misc, transactions, task lock
    # =====================================================================
    def is_approval_granted(self, approval_id: str) -> bool: ...
    def reset_no_progress_streak(self, task_id: str) -> None: ...
    def increment_no_progress_streak(self, task_id: str) -> int: ...
    def next_loop_id(self, task_id: str) -> int: ...

    def acquire_task_lock(
        self,
        task_id: str,
        owner: str,
        *,
        stale_threshold_seconds: int,
        steal: bool = False,
    ) -> Literal["acquired", "reentrant", "held_live", "held_stale", "stolen"]: ...
    """v0.5b.0 (PRD §5.1.1): Task lock with stale detection and steal.

    Outcomes:
      * ``acquired``    — no prior owner; lock now held by ``owner``.
      * ``reentrant``   — same hostname:pid as the existing owner; pass-through.
      * ``held_live``   — held by another live process; CALLER MUST exit 3.
      * ``held_stale``  — stale lock and ``steal=False``; CALLER MUST exit 6.
      * ``stolen``      — ``steal=True`` was passed; emits ``lock_stolen`` event.

    The owner string MUST be ``f"{hostname}:{pid}:{uuid4_hex8}"``. Stale
    detection compares against ``stale_threshold_seconds`` provided by the
    caller (env ``HUNGERLOOP_LOCK_STALE_SEC`` or ``--lock-stale-sec``)."""

    def release_task_lock(self, task_id: str, owner: str) -> None: ...
    """No-op when the current owner doesn't match — defends against
    double-release after a steal. Clean shutdown calls this in the same
    transaction that persists ``StopReport`` (PRD §5.1.1)."""

    def transaction(self) -> AbstractContextManager[Any]: ...
    """Cross-cutting writes (CommitManager) execute inside ``with
    repo.transaction(): ...``. InMemoryRepository returns a no-op context;
    SQLiteRepository returns ``BEGIN IMMEDIATE``/``COMMIT`` (ADR-001)."""
