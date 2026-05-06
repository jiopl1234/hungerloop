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

    # ----- Memory / skill -----------------------------------------------
    MEMORY_CANDIDATE_EMITTED = "memory_candidate_emitted"
    SKILL_CARD_EMITTED = "skill_card_emitted"
