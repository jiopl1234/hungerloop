"""Success classification for persisted evidence payloads."""
from __future__ import annotations

from hungerloop.models.enums import EvidenceType


def is_successful_evidence_payload(
    evidence_type: EvidenceType | str,
    payload: dict[str, object],
) -> bool:
    """Return whether an evidence row should count as successful progress."""
    ev_type = (
        evidence_type.value if isinstance(evidence_type, EvidenceType) else evidence_type
    )
    if ev_type == EvidenceType.MODEL_CALL.value:
        return True
    if ev_type == EvidenceType.MODEL_ERROR.value:
        return False
    if ev_type == EvidenceType.TOOL_CALL.value:
        return payload.get("success") is True
    if ev_type == EvidenceType.SANDBOX_RUN.value:
        return payload.get("exit_code") == 0 and payload.get("timed_out") is False
    return False
