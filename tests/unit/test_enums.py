# tests/unit/test_enums.py
from hungerloop.models.enums import (
    AcceptanceCheckType,
    DecayType,
    HungerItemStatus,
    HungerItemType,
    LoopPhase,
    StopReason,
    ValidationVerdict,
)


def test_stop_reason_includes_all_values() -> None:
    values = {e.value for e in StopReason}
    assert values == {
        "done",
        "hunger_expired",
        "blocked",
        "human_required",
        "human_paused",
        "safety_stop",
        "error",
    }


def test_validation_verdict_values() -> None:
    assert ValidationVerdict.PASS.value == "pass"
    assert ValidationVerdict.PARTIAL.value == "partial"
    assert ValidationVerdict.FAIL.value == "fail"


def test_acceptance_check_type_values() -> None:
    assert AcceptanceCheckType.FILE_EXISTS.value == "file_exists"
    assert AcceptanceCheckType.SHELL_EXIT_ZERO.value == "shell_exit_zero"
    assert AcceptanceCheckType.EVIDENCE_COUNT_MIN.value == "evidence_count_min"
    assert AcceptanceCheckType.ARTIFACT_TYPE_EXISTS.value == "artifact_type_exists"
    assert AcceptanceCheckType.HUMAN_APPROVAL.value == "human_approval"
    assert AcceptanceCheckType.LLM_JUDGE.value == "llm_judge"


def test_hunger_item_status_values() -> None:
    assert {e.value for e in HungerItemStatus} == {
        "open",
        "working",
        "blocked",
        "paused",
        "closed",
        "validated_satisfied",
    }


def test_decay_type_values() -> None:
    assert DecayType.LINEAR.value == "linear"
    assert DecayType.LOOP_COUNT.value == "loop_count"
    assert DecayType.STAGE_BASED.value == "stage_based"


def test_loop_phase_values() -> None:
    assert LoopPhase.EXPLORE.value == "explore"
    assert LoopPhase.EXPLOIT.value == "exploit"
    assert LoopPhase.COOLDOWN.value == "cooldown"


def test_hunger_item_type_values() -> None:
    assert HungerItemType.GOAL_GAP.value == "goal_gap"
    assert HungerItemType.MEMORY_CONSOLIDATION.value == "memory_consolidation"


def test_str_enum_equality() -> None:
    """Verify (str, Enum) inheritance gives transparent string equality."""
    assert StopReason.DONE == "done"
    assert ValidationVerdict.FAIL == "fail"
    assert HungerItemStatus.BLOCKED == "blocked"
    assert AcceptanceCheckType.FILE_EXISTS == "file_exists"
    assert DecayType.LOOP_COUNT == "loop_count"
    assert LoopPhase.EXPLORE == "explore"
    assert HungerItemType.GOAL_GAP == "goal_gap"
