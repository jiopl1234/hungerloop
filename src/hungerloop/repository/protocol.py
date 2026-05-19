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
from datetime import datetime
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
from hungerloop.models.memory import MemoryCandidate, PromotedMemory
from hungerloop.models.mission import (
    Mission,
    MissionFeature,
    MissionFeatureStatus,
    MissionPhase,
    MissionPhaseStatus,
)
from hungerloop.models.planning import LoopPlan
from hungerloop.models.skill import ActiveSkillCard, SkillCard, SkillCardCandidate
from hungerloop.models.task import TaskRecord
from hungerloop.models.tracing import LoopTrace, StopReport
from hungerloop.models.usage import UsageSnapshot
from hungerloop.models.validation import ValidationReport
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationAssertionStatus,
    ValidationContract,
)
from hungerloop.models.worker import AgentSpec, WorkerHandoff, WorkerResult


class RepositoryProtocol(Protocol):
    """Single persistence protocol for all HungerLoop services (ADR-006)."""

    # =====================================================================
    # Section 0 — Task metadata
    # =====================================================================
    def create_task(self, task_id: str, raw_goal: str) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def task_exists(self, task_id: str) -> bool: ...
    def list_task_ids(self) -> list[str]: ...
    """New in v0.5e.0 (E0-11): enumerate every persisted task id for
    ``hungerloop memory list --all-tasks``. Stable order (creation
    order on InMemory; rowid asc on SQLite)."""

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

    def list_candidates_for_task(self, task_id: str) -> list[CandidateState]: ...
    """New in v0.5d.0 (D0-01 / FR-1): public read path for repair-state
    and CLI surfaces that previously reached into ``InMemoryRepository.
    _candidates``. Returns candidates ordered by loop_id ASC."""

    def get_candidate(self, candidate_id: str) -> CandidateState | None: ...
    """New in v0.5d.0 (D0-01 / FR-1): single-candidate lookup; returns
    None when the id is unknown."""

    def candidate_status(
        self, candidate_id: str
    ) -> Literal["pending", "committed", "rejected", "missing"]: ...
    """New in v0.5d.1 (D1-08 / FR-1): expose the candidate-row status so
    the D12 detector can cross-reference workspace dirs against
    terminal rows without reaching into ``InMemoryRepository`` private
    sets. Returns ``"missing"`` for unknown ids so callers don't need a
    second null check."""

    # =====================================================================
    # Section 3 — Validation
    # =====================================================================
    def save_validation_report(self, report: ValidationReport) -> None: ...
    def add_failure_from_validation(self, report: ValidationReport) -> None: ...

    def get_validation_report(
        self, validation_id: str
    ) -> ValidationReport | None: ...
    """New in v0.5d.0 (D0-01 / FR-1): single-report lookup; returns None
    when the id is unknown."""

    def validation_exists(self, validation_id: str) -> bool: ...
    """New in v0.5d.0 (D0-01 / FR-1): cheap existence check used by the
    repair-state D7 detector to flag dangling accepted_checks rows."""

    def iter_accepted_checks(
        self, task_id: str
    ) -> list[dict[str, object]]: ...
    """New in v0.5d.0 (D0-01 / FR-1): list of accepted_check rows
    persisted by ``CommitManager.promote``. Each dict carries
    ``check_key``, ``hunger_item_id``, ``check_index``,
    ``accepted_at_loop``, ``validation_id``, ``evidence_id``."""

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

    def list_successful_tool_call_evidence(
        self, task_id: str
    ) -> list[dict[str, object]]: ...
    """New in v0.5e.1 (E1-05 / PRD §18.4): list every successful
    ``tool_call`` evidence row for ``task_id`` so ``derive_tools`` can
    walk them in chronological order. Each dict carries
    ``loop_id``, ``payload`` (with ``tool_name``), and the standard
    evidence fields. Used only by the derivation helpers; CLI surfaces
    can still call ``count_evidence_by_type`` for cardinality."""

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

    def save_worker_handoff(self, handoff: WorkerHandoff) -> str: ...
    """New in v0.6 M2 (REQ-M2-010): persist a WorkerHandoff row."""

    def list_worker_handoffs(
        self,
        task_id: str,
        *,
        since_loop_id: int | None = None,
        limit: int | None = None,
    ) -> list[WorkerHandoff]: ...
    """New in v0.6 M2 (REQ-M2-010): list persisted handoffs for a task."""

    def get_last_worker_handoff(
        self,
        task_id: str,
        agent_id: str,
        *,
        before_loop_id: int,
    ) -> WorkerHandoff | None: ...
    """New in v0.6 M2 (REQ-M2-010): latest prior handoff for one agent."""

    def get_last_worker_result(
        self,
        task_id: str,
        agent_id: str,
        before_loop_id: int,
    ) -> WorkerResult | None: ...
    """New in v0.5f.0: latest prior result for one agent before a loop.

    This is intentionally agent-scoped. Future multi-agent loop memory should
    add an explicit cross-agent read path rather than broadening this method
    silently.
    """

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

    def get_loop_trace(self, task_id: str, loop_id: int) -> LoopTrace | None: ...
    """New in v0.5d.1 (D0-01 / FR-1): single-trace lookup keyed by
    ``(task_id, loop_id)``. Returns ``None`` when the trace is missing
    — used by the repair-state D13 detector to flag orphaned event
    rows that reference a non-existent loop."""

    def aggregate_evidence_usage(self, task_id: str) -> UsageSnapshot: ...
    """New in v0.5d.1 (D1-07): recompute usage totals by walking the
    evidence rows for ``task_id``. Used by the D11 detector to
    cross-check ``usage_snapshots`` against the source-of-truth
    evidence rows; the returned :class:`UsageSnapshot` is *not*
    persisted unless the caller (``repair-state --apply``) explicitly
    writes it back."""

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

    def list_events(
        self,
        task_id: str,
        *,
        since_loop: int | None = None,
        until_loop: int | None = None,
        event_types: list[str] | None = None,
        include_global: bool = False,
    ) -> list[dict[str, object]]: ...
    """Per-task event rows in append order.

    The shipped behaviour (no kwargs) is unchanged: global events are
    excluded so ``trace export <task_id>`` stays a strict per-task
    projection.

    v0.5d.1 (D0-01 / FR-1) adds optional filtering:

    * ``since_loop`` / ``until_loop`` — inclusive ``loop_id`` bounds.
    * ``event_types`` — restrict to rows whose ``event_type`` is in
      this list (raw ``EventType.value`` strings).
    * ``include_global`` — when ``True``, also include rows with
      ``task_id IS NULL`` so ``hungerloop trace export --include-global``
      can surface unknown_model_pricing and similar global events."""

    # =====================================================================
    # Section 7 — Memory / Skill
    # =====================================================================
    def save_memory_candidate(self, candidate: MemoryCandidate) -> None: ...
    def list_memory_candidates(self, task_id: str) -> list[MemoryCandidate]: ...
    def get_memory_candidate(
        self, candidate_id: str
    ) -> MemoryCandidate | None: ...
    """New in v0.5e.0 (E0-03 / FR-1): single-candidate lookup; returns
    None when the id is unknown."""

    def count_committed_references(self, candidate_id: str) -> int: ...
    """Used by ``MemoryManager.non_volatile`` predicate (§19.2)."""

    def save_promoted_memory(self, memory: PromotedMemory) -> None: ...
    """New in v0.5e.0 (E0-03 / FR-21): persist an ``ApprovalEngine``
    promotion record."""

    def list_promoted_memories(
        self, task_id: str | None = None
    ) -> list[PromotedMemory]: ...
    """New in v0.5e.0: enumerate promoted memories. ``task_id=None``
    returns every promoted row (across tasks) so global retrieval
    surfaces in v0.6 can read the same path."""

    def get_promoted_memory(self, memory_id: str) -> PromotedMemory | None: ...
    """New in v0.5e.0: single-memory lookup."""

    def save_skill_card(self, card: SkillCard) -> None: ...
    def list_skill_cards(self, task_id: str | None = None) -> list[SkillCard]: ...

    # ----- v0.5e.1 skill lifecycle (PRD §18 / E1-03) ---------------------
    def save_skill_card_candidate(
        self, candidate: SkillCardCandidate
    ) -> None: ...
    """Insert/update an auto-generated skill candidate awaiting review."""

    def get_skill_card_candidate(
        self, skill_candidate_id: str
    ) -> SkillCardCandidate | None: ...

    def list_skill_card_candidates(
        self,
        *,
        task_id: str | None = None,
        state: str | None = None,
    ) -> list[SkillCardCandidate]: ...
    """Filter by task and/or state. Default returns every candidate
    (state=None means no filter; ``"all"`` is equivalent)."""

    def save_active_skill_card(self, skill: ActiveSkillCard) -> None: ...
    """Insert/update a promoted skill (FR-3)."""

    def get_active_skill_card(self, skill_id: str) -> ActiveSkillCard | None: ...

    def list_active_skill_cards(
        self, *, state: str | None = None
    ) -> list[ActiveSkillCard]: ...
    """List every active/deprecated skill. State filter is optional."""

    # =====================================================================
    # Section 8 — Mission runtime
    # =====================================================================
    def save_mission(self, mission: Mission) -> None: ...
    def get_mission(self, task_id: str) -> Mission | None: ...

    def save_mission_phase(self, phase: MissionPhase) -> None: ...
    def update_phase_status(
        self,
        phase_id: str,
        status: MissionPhaseStatus,
        *,
        completed_at: datetime | None = None,
    ) -> None: ...
    def list_mission_phases(self, mission_id: str) -> list[MissionPhase]: ...

    def save_mission_feature(self, feature: MissionFeature) -> None: ...
    def update_feature_status(
        self,
        feature_id: str,
        status: MissionFeatureStatus,
        *,
        completed_at: datetime | None = None,
    ) -> None: ...
    def list_mission_features(
        self,
        mission_id: str | None = None,
        phase_id: str | None = None,
    ) -> list[MissionFeature]: ...
    def list_features_for_phase(self, phase_id: str) -> list[MissionFeature]: ...

    def save_validation_contract(self, contract: ValidationContract) -> None: ...
    def get_validation_contract(
        self, mission_id: str
    ) -> ValidationContract | None: ...
    def save_validation_assertion(self, assertion: ValidationAssertion) -> None: ...
    def update_assertion_status(
        self,
        assertion_id: str,
        status: ValidationAssertionStatus,
        *,
        validated_at_loop: int | None = None,
        evidence_ids: list[str] | None = None,
    ) -> None: ...
    def list_validation_assertions(
        self,
        mission_id: str | None = None,
        phase_id: str | None = None,
    ) -> list[ValidationAssertion]: ...

    # =====================================================================
    # Section 9 — Approvals, misc, transactions, task lock
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

    def get_task_lock(self, task_id: str) -> dict[str, object] | None: ...
    """New in v0.5d.0 (D0-01 / FR-1): public lock inspector. Returns
    ``{"owner": str, "locked_at": datetime}`` or ``None`` when no lock
    is held. Used by the repair-state D6 detector and by ``hungerloop
    status`` to surface stale-lock conditions."""

    def transaction(self) -> AbstractContextManager[Any]: ...
    """Cross-cutting writes (CommitManager) execute inside ``with
    repo.transaction(): ...``. InMemoryRepository returns a no-op context;
    SQLiteRepository returns ``BEGIN IMMEDIATE``/``COMMIT`` (ADR-001)."""
