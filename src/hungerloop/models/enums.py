from enum import Enum


class StopReason(str, Enum):
    DONE = "done"
    HUNGER_EXPIRED = "hunger_expired"
    BLOCKED = "blocked"
    HUMAN_REQUIRED = "human_required"
    HUMAN_PAUSED = "human_paused"
    SAFETY_STOP = "safety_stop"
    ERROR = "error"


class ValidationVerdict(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"


class AcceptanceCheckType(str, Enum):
    FILE_EXISTS = "file_exists"
    SHELL_EXIT_ZERO = "shell_exit_zero"
    EVIDENCE_COUNT_MIN = "evidence_count_min"
    ARTIFACT_TYPE_EXISTS = "artifact_type_exists"
    HUMAN_APPROVAL = "human_approval"
    LLM_JUDGE = "llm_judge"


class HungerItemStatus(str, Enum):
    OPEN = "open"
    WORKING = "working"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CLOSED = "closed"
    VALIDATED_SATISFIED = "validated_satisfied"


class HungerItemType(str, Enum):
    GOAL_GAP = "goal_gap"
    MEMORY_CONSOLIDATION = "memory_consolidation"


class DecayType(str, Enum):
    LINEAR = "linear"
    LOOP_COUNT = "loop_count"
    STAGE_BASED = "stage_based"


class LoopPhase(str, Enum):
    EXPLORE = "explore"
    EXPLOIT = "exploit"
    COOLDOWN = "cooldown"


class EvidenceType(str, Enum):
    """Taxonomy for evidence rows.

    New in v0.5a (PRD §3.3 / §28 M7). Freezes the set of type strings so
    repository implementations and ``count_evidence_by_type`` callers stop
    relying on ad-hoc literals.
    """

    SANDBOX_RUN = "sandbox_run"
    MODEL_CALL = "model_call"
    MODEL_ERROR = "model_error"
    VALIDATION_CHECK = "validation_check"
    TOOL_CALL = "tool_call"
    HUMAN_INPUT = "human_input"
