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
