"""Canonical event vocabulary for `repo.append_event` (PRD §22.8).

The :class:`EventType` enum freezes the event names that the system writes
to the ``events`` table. Every callsite passes an enum member instead of a
literal string; ``mypy --strict`` catches typos at the boundary.

Stored representation: each repository implementation calls ``.value`` on
the enum and writes the string. This keeps SQLite ``events.event_type`` a
plain TEXT column and avoids custom JSON encoders.

**Stability rules**

- Adding new members is **additive** and does not bump the schema.
- Renaming or removing a member requires a major version bump on the event
  vocabulary plus a SQLite migration if persisted rows reference the old
  name (and coordinated test rewrites). Treat the strings here as a wire
  contract.
- New names must follow ``[a-z][a-z0-9_]*`` (snake_case, lowercase).
"""
from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """Stable event-type vocabulary stored in the ``events`` table."""

    # ----- Loop lifecycle ------------------------------------------------
    LOOP_STARTED = "loop_started"
    LOOP_PLANNED = "loop_planned"  # v0.5d.0 (PRD §7.3)
    LOOP_COMMITTED = "loop_committed"
    LOOP_REJECTED = "loop_rejected"

    # ----- Worker invocations (v0.5d.0) ---------------------------------
    WORKER_STARTED = "worker_started"
    WORKER_FINISHED = "worker_finished"
    WORKER_FAILED = "worker_failed"
    CONTEXT_TRUNCATED = "context_truncated"
    WORKER_HANDOFF_EMITTED = "worker.handoff_emitted"
    WORKER_HANDOFF_RECEIVED = "worker.handoff_received"
    WORKER_HANDOFF_BLOCKER_RECORDED = "worker.handoff_blocker_recorded"
    HANDOFF_BLOCKER_ON_CLOSED_ITEM = "HANDOFF_BLOCKER_ON_CLOSED_ITEM"
    WORKER_ASSIGNMENT_STARTED = "worker.assignment_started"
    WORKER_ASSIGNMENT_COMPLETED = "worker.assignment_completed"
    WORKER_ASSIGNMENT_FAILED = "worker.assignment_failed"
    WORKER_ASSIGNMENT_SKIPPED = "worker.assignment_skipped"
    WORKER_ASSIGNMENT_RETRIED = "worker.assignment_retried"
    WORKSPACE_WRITE_COLLISION = "WORKSPACE_WRITE_COLLISION"
    PLANNER_CYCLE_DETECTED = "PLANNER_CYCLE_DETECTED"

    # ----- Model calls (v0.5d.0) ----------------------------------------
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_SUCCEEDED = "model_call_succeeded"
    MODEL_CALL_FAILED = "model_call_failed"
    MODEL_AUTH_REQUIRED = "model_auth_required"
    MODEL_RATE_LIMITED = "model_rate_limited"

    # ----- Tool calls (v0.5d.0) -----------------------------------------
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_SUCCEEDED = "tool_call_succeeded"
    TOOL_CALL_FAILED = "tool_call_failed"

    # ----- Validation + check-level verdicts (v0.5d.0) ------------------
    VALIDATION_STARTED = "validation_started"
    VALIDATION_FINISHED = "validation_finished"
    VALIDATION_PIPELINE_STARTED = "validation.pipeline_started"
    VALIDATION_PIPELINE_COMPLETED = "validation.pipeline_completed"
    VALIDATION_SCRUTINY_STARTED = "validation.scrutiny_started"
    VALIDATION_SCRUTINY_COMPLETED = "validation.scrutiny_completed"
    VALIDATION_SCRUTINY_SKIPPED = "validation.scrutiny_skipped"
    VALIDATION_SCRUTINY_TIMEOUT = "validation.scrutiny_timeout"
    VALIDATION_USER_TESTING_SKIPPED = "validation.user_testing_skipped"
    CHECK_PASSED = "check_passed"
    CHECK_FAILED = "check_failed"
    CHECK_REGRESSED = "check_regressed"

    # ----- Candidate decisions (v0.5d.0) --------------------------------
    CANDIDATE_CREATED = "candidate_created"
    CANDIDATE_COMMITTED = "candidate_committed"
    CANDIDATE_REJECTED = "candidate_rejected"

    # ----- Hunger -------------------------------------------------------
    HUNGER_RESUMED = "hunger_resumed"
    HUNGER_FROZEN = "hunger_frozen"
    HUNGER_REFILLED = "hunger_refilled"
    HUMAN_UNBLOCKED_HUNGER_ITEM = "human_unblocked_hunger_item"
    COST_CEILING_RAISED = "cost_ceiling_raised"

    # ----- Budgeted refinement (v0.5f.4) --------------------------------
    REFINEMENT_TIER_STARTED = "refinement_tier_started"
    REFINEMENT_ITEMS_ADDED = "refinement_items_added"
    REFINEMENT_BUDGET_EXHAUSTED = "refinement_budget_exhausted"

    # ----- Stops --------------------------------------------------------
    SAFETY_STOP = "safety_stop"
    HUMAN_REQUIRED = "human_required"
    STOP_REPORT_CREATED = "stop_report_created"  # v0.5d.0 (PRD §7.3)
    ERROR = "error"  # v0.5d.0 — orchestrator caught a non-stop exception

    # ----- Cost / pricing -----------------------------------------------
    COST_RECONCILIATION = "cost_reconciliation"  # PRD §8.7.1
    UNKNOWN_MODEL_PRICING = "unknown_model_pricing"

    # ----- Locks / repair -----------------------------------------------
    LOCK_STOLEN = "lock_stolen"  # PRD §5.1.1
    REPAIR_STATE_ACTION = "repair_state_action"  # PRD §16.3

    # ----- Schema / migration -------------------------------------------
    MIGRATION_APPLIED = "migration_applied"
    MIGRATION_FAILED = "migration_failed"

    # ----- Memory / skill -----------------------------------------------
    MEMORY_CANDIDATE_EMITTED = "memory_candidate_emitted"
    # v0.5e.0 — memory lifecycle (PRD §19 / FR-22). Each row corresponds
    # to a single CLI verb; the orchestrator never emits these directly.
    MEMORY_CANDIDATE_APPROVED = "memory_candidate_approved"
    MEMORY_CANDIDATE_REJECTED = "memory_candidate_rejected"
    MEMORY_CANDIDATE_DEFERRED = "memory_candidate_deferred"
    MEMORY_CANDIDATE_EXPIRED = "memory_candidate_expired"
    MEMORY_PROMOTED = "memory_promoted"
    SKILL_CARD_EMITTED = "skill_card_emitted"
    # v0.5e.1 — skill lifecycle (PRD §18 / FR-16). The shipped
    # SKILL_CARD_EMITTED stays in the enum but is no longer emitted by
    # SkillManager; the new vocabulary mirrors memory's lifecycle shape.
    SKILL_CARD_CANDIDATE_CREATED = "skill_card_candidate_created"
    SKILL_CARD_ACTIVATED = "skill_card_activated"
    SKILL_CARD_REJECTED = "skill_card_rejected"
    SKILL_CARD_IMPORTED = "skill_card_imported"
