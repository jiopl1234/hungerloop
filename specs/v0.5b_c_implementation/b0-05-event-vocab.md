# b0-05 · `EventType` enum + typed `append_event`

**Spec**: §7 (enum part). **PRD**: §22.8. **Release**: v0.5b.0.

## Goal

Freeze the event vocabulary as a typed enum so future metrics/dashboards have a stable contract. Forward-compatible: new types are additive only.

## Files to touch

- **NEW** `src/hungerloop/models/events.py` — `EventType` enum + helper for serialization.
- `src/hungerloop/repository/protocol.py` — change `append_event(event_type: str, ...)` to `append_event(event_type: EventType, ...)`.
- `src/hungerloop/repository/in_memory_repo.py` — accept `EventType`, store its `.value`.
- All callers of `append_event` — migrate string literals to enum members.
- **NEW** `tests/unit/test_event_vocab.py`.

## Enum (initial set, v0.5b.0)

```python
class EventType(str, Enum):
    LOOP_STARTED = "loop_started"
    LOOP_COMMITTED = "loop_committed"
    LOOP_REJECTED = "loop_rejected"

    HUNGER_RESUMED = "hunger_resumed"
    HUNGER_FROZEN = "hunger_frozen"
    HUNGER_REFILLED = "hunger_refilled"

    SAFETY_STOP = "safety_stop"
    HUMAN_REQUIRED = "human_required"

    COST_RECONCILIATION = "cost_reconciliation"        # b1-01
    UNKNOWN_MODEL_PRICING = "unknown_model_pricing"

    LOCK_STOLEN = "lock_stolen"                        # b0-04
    REPAIR_STATE_ACTION = "repair_state_action"        # b0-03

    MEMORY_CANDIDATE_EMITTED = "memory_candidate_emitted"
    SKILL_CARD_EMITTED = "skill_card_emitted"
```

## Checklist

- [ ] Create `models/events.py` with the enum above.
- [ ] Update `RepositoryProtocol.append_event` signature: `event_type: EventType`.
- [ ] Update `InMemoryRepository.append_event` to call `.value` on the enum before storing in `_events` (or store the enum directly — preference: store `.value` for SQL parity).
- [ ] Run `grep -rn 'append_event(' src/ tests/` → migrate every string literal call to the matching enum.
  - Existing call sites (verify with grep): `cli/run_cmd.py` (`hunger_resumed`), `cli/hunger_cmd.py` (any), `services/openai_model_client.py` (`unknown_model_pricing` if present), tests.
- [ ] **Backward-compat shim**: tests that assert `e["event_type"] == "hunger_resumed"` keep working because the stored value is still the string. No test rewrites needed if we store `.value`.
- [ ] mypy --strict will catch any unmigrated literal — that's the migration safety net.

## Tests (`test_event_vocab.py`)

- [ ] `test_event_type_enum_values_match_legacy_strings` — every legacy string used in v0.5a tests has a corresponding enum value.
- [ ] `test_append_event_accepts_enum_member`
- [ ] `test_append_event_rejects_raw_string` — mypy contract; runtime accepts via `str` Enum coercion, but `.value` is what's stored.
- [ ] `test_event_type_membership_is_stable` — assert the enum has at least the v0.5b.0 entries (regression guard against accidental rename).

## Done when

- [ ] All 4 tests pass.
- [ ] `grep -rn 'append_event(' src/` returns no string-literal first arguments.
- [ ] `mypy --strict` clean.
- [ ] PRD §22.8 references this enum.

## Notes

- Storing `.value` (not the enum) keeps the SQLite `events.event_type` column as plain TEXT and survives JSON serialization without custom encoders.
- Adding a new event type later: just add the enum member. Removing or renaming requires a schema migration AND a major version on the event vocabulary (deferred).
