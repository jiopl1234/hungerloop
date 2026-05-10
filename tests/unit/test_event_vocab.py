"""EventType vocabulary tests (PRD §22.8).

Pin the wire-contract: every enum member is a snake_case string, the
v0.5a-era literal strings still resolve through the enum, and storage
goes through ``.value`` so SQLite TEXT columns and JSON output stay
plain.
"""
from __future__ import annotations

import re

from hungerloop.models.events import EventType
from hungerloop.repository.in_memory_repo import InMemoryRepository

# v0.5a → v0.5b vocabulary preservation. The strings on the right are the
# wire-contract that existing event-table rows reference; renaming any of
# these requires a coordinated migration.
LEGACY_EVENT_NAMES = {
    "hunger_refilled",
    "hunger_resumed",
    "hunger_frozen",
    "human_unblocked_hunger_item",
    "cost_ceiling_raised",
    "unknown_model_pricing",
}


def test_every_event_value_is_snake_case() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]*$")
    for member in EventType:
        assert pattern.match(member.value), (
            f"{member.name} value {member.value!r} is not snake_case"
        )


def test_v0_5a_literal_strings_have_enum_homes() -> None:
    """Renaming a legacy string would orphan persisted rows; this test
    pins the v0.5a → v0.5b carry-forward."""
    members_by_value = {m.value for m in EventType}
    missing = LEGACY_EVENT_NAMES - members_by_value
    assert not missing, f"Legacy event names without enum homes: {missing}"


def test_v0_5b_additions_present() -> None:
    """The v0.5b PRD adds these as part of §22.8."""
    expected = {
        "loop_started",
        "loop_committed",
        "loop_rejected",
        "safety_stop",
        "human_required",
        "cost_reconciliation",
        "lock_stolen",
        "repair_state_action",
        "memory_candidate_emitted",
        "skill_card_emitted",
    }
    actual = {m.value for m in EventType}
    assert expected <= actual


def test_v0_5d_additions_present() -> None:
    """v0.5d.0 (PRD §7.3) extends the lifecycle vocabulary with the
    fine-grained per-loop event names emitted by the orchestrator,
    worker runtime, model client, tool harness, and validation gate.
    All additive — no shipped value renamed.
    """
    expected = {
        # Loop / worker
        "loop_planned",
        "worker_started",
        "worker_finished",
        "worker_failed",
        # Model calls
        "model_call_started",
        "model_call_succeeded",
        "model_call_failed",
        "model_auth_required",
        "model_rate_limited",
        # Tool calls
        "tool_call_started",
        "tool_call_succeeded",
        "tool_call_failed",
        # Validation
        "validation_started",
        "validation_finished",
        "check_passed",
        "check_failed",
        "check_regressed",
        # Candidate lifecycle
        "candidate_created",
        "candidate_committed",
        "candidate_rejected",
        # Stop
        "stop_report_created",
        "error",
    }
    actual = {m.value for m in EventType}
    assert expected <= actual


def test_v0_5e_memory_lifecycle_additions_present() -> None:
    """v0.5e.0 (PRD §19 / FR-22): memory lifecycle audit events.

    The shipped ``memory_candidate_emitted`` is unchanged; the new
    rows fire from the CLI's approve/reject/defer/expire commands
    and from ApprovalEngine's promotion path.
    """
    expected = {
        "memory_candidate_approved",
        "memory_candidate_rejected",
        "memory_candidate_deferred",
        "memory_candidate_expired",
        "memory_promoted",
    }
    actual = {m.value for m in EventType}
    assert expected <= actual


def test_v0_5e_skill_lifecycle_additions_present() -> None:
    """v0.5e.1 (PRD §18 / FR-16): skill lifecycle audit events.

    The shipped ``skill_card_emitted`` stays in the enum but is no
    longer fired by SkillManager — the candidate/active split owns
    the audit trail going forward.
    """
    expected = {
        "skill_card_candidate_created",
        "skill_card_activated",
        "skill_card_rejected",
        "skill_card_imported",
    }
    actual = {m.value for m in EventType}
    assert expected <= actual


def test_v0_5f_budgeted_refinement_additions_present() -> None:
    """v0.5f.4 adds refinement lifecycle audit events."""
    expected = {
        "refinement_tier_started",
        "refinement_items_added",
        "refinement_budget_exhausted",
    }
    actual = {m.value for m in EventType}
    assert expected <= actual


def test_no_shipped_event_value_renamed() -> None:
    """Wire-contract regression net (PRD §7.2) — every value that ever
    shipped to operators must remain in the enum. Adding to this set
    when a new release ships is a one-line change; removing from it is
    forbidden without a coordinated migration.
    """
    shipped_values_through_v0_5b_c = {
        "loop_started",
        "loop_committed",
        "loop_rejected",
        "hunger_resumed",
        "hunger_frozen",
        "hunger_refilled",
        "human_unblocked_hunger_item",
        "cost_ceiling_raised",
        "safety_stop",
        "human_required",
        "cost_reconciliation",
        "unknown_model_pricing",
        "lock_stolen",
        "repair_state_action",
        "memory_candidate_emitted",
        "skill_card_emitted",
    }
    actual = {m.value for m in EventType}
    missing = shipped_values_through_v0_5b_c - actual
    assert not missing, f"Shipped event values dropped: {missing}"


def test_append_event_stores_string_value_not_enum() -> None:
    """Storage uses ``.value`` so SQL TEXT columns stay plain."""
    repo = InMemoryRepository()
    repo.append_event(EventType.HUNGER_RESUMED, {}, task_id="t1")
    assert repo._events[0]["event_type"] == "hunger_resumed"
    # And not, e.g., the enum repr or anything else surprising:
    assert isinstance(repo._events[0]["event_type"], str)


def test_event_type_is_str_enum_for_json_friendliness() -> None:
    """Pydantic v2 + json.dumps both treat ``str``-Enums as strings; this
    pins the inheritance choice in case someone tries to drop ``str``."""
    assert issubclass(EventType, str)
    assert EventType.HUNGER_FROZEN == "hunger_frozen"
