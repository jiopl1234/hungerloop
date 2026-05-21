"""Shared mission cockpit rendering for ``mission status`` and ``report``."""
from __future__ import annotations

from dataclasses import dataclass

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion
from hungerloop.repository.protocol import RepositoryProtocol


@dataclass(frozen=True)
class _StatusRow:
    id: str
    title: str
    status: str
    symbol: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "symbol": self.symbol,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class _MissionCockpit:
    mission: Mission
    phases: list[_StatusRow]
    active_phase: MissionPhase | None
    features_in_active_phase: list[_StatusRow]
    validation_contract: dict[str, int]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "mission": self.mission.model_dump(mode="json"),
            "phases": [row.to_json() for row in self.phases],
            "features_in_active_phase": [
                row.to_json() for row in self.features_in_active_phase
            ],
            "validation_contract": dict(self.validation_contract),
        }


def build_mission_cockpit(
    repo: RepositoryProtocol,
    mission_obj: Mission,
) -> _MissionCockpit:
    """Collect mission status rows for text and JSON renderers."""
    features = repo.list_mission_features(mission_id=mission_obj.mission_id)
    if not features and mission_obj.features:
        features = mission_obj.features
    features_by_phase: dict[str, list[MissionFeature]] = {}
    for feature in features:
        features_by_phase.setdefault(feature.phase_id, []).append(feature)

    assertions = repo.list_validation_assertions(mission_id=mission_obj.mission_id)
    assertions_by_phase: dict[str, list[ValidationAssertion]] = {}
    for assertion in assertions:
        assertions_by_phase.setdefault(assertion.phase_id, []).append(assertion)

    phases = repo.list_mission_phases(mission_obj.mission_id)
    if not phases and mission_obj.phases:
        phases = mission_obj.phases
    phase_rows = [
        _StatusRow(
            id=phase.phase_id,
            title=phase.title,
            status=phase.status,
            symbol=_status_symbol(phase.status),
            detail=_phase_detail(
                phase,
                features_by_phase.get(phase.phase_id, []),
                assertions_by_phase.get(phase.phase_id, []),
            ),
        )
        for phase in phases
    ]
    active_phase = _active_phase(phases)
    active_features = (
        features_by_phase.get(active_phase.phase_id, []) if active_phase else []
    )
    feature_rows = [
        _StatusRow(
            id=feature.feature_id,
            title=feature.title,
            status=feature.status,
            symbol=_status_symbol(feature.status),
            detail=_feature_detail(feature),
        )
        for feature in active_features
    ]
    return _MissionCockpit(
        mission=mission_obj,
        phases=phase_rows,
        active_phase=active_phase,
        features_in_active_phase=feature_rows,
        validation_contract=repo.count_validation_contract_summary(
            mission_obj.mission_id
        ),
    )


def render_mission_cockpit(cockpit: _MissionCockpit) -> str:
    """Render the human mission cockpit specified by REQ-M6-020."""
    lines = [
        f"Mission: {cockpit.mission.mission_id} — {cockpit.mission.title}",
        "Phases:",
    ]
    for row in cockpit.phases:
        detail = f"            {row.detail}" if row.detail else ""
        lines.append(f"  {row.symbol} {row.id} — {row.title}{detail}")

    lines.append("")
    lines.append("Features in active phase:")
    if cockpit.features_in_active_phase:
        for row in cockpit.features_in_active_phase:
            detail = f"           {row.detail}" if row.detail else ""
            lines.append(f"  {row.symbol} {row.id}   {row.title}{detail}")
    else:
        lines.append("  (none)")

    summary = cockpit.validation_contract
    lines.extend(
        [
            "",
            "Validation contract:",
            (
                f"  Pending: {summary['pending']}    "
                f"Passed: {summary['passed']}    "
                f"Failed: {summary['failed']}    "
                f"Blocked: {summary['blocked']}"
            ),
        ]
    )
    return "\n".join(lines)


def _active_phase(phases: list[MissionPhase]) -> MissionPhase | None:
    for status in ("validating", "in_progress"):
        for phase in phases:
            if phase.status == status:
                return phase
    for phase in phases:
        if phase.status == "pending":
            return phase
    return phases[-1] if phases else None


def _status_symbol(status: str) -> str:
    if status in {"done", "passed"}:
        return "[✓]"
    if status in {"in_progress", "validating"}:
        return "[→]"
    if status in {"failed", "blocked"}:
        return "[×]"
    return "[ ]"


def _phase_detail(
    phase: MissionPhase,
    features: list[MissionFeature],
    assertions: list[ValidationAssertion],
) -> str:
    if phase.status == "done":
        loops = [
            assertion.validated_at_loop
            for assertion in assertions
            if assertion.validated_at_loop is not None
        ]
        if loops:
            return f"validated at loop {max(loops)}"
        return "done"
    if phase.status == "validating":
        done_features = sum(1 for feature in features if feature.status == "done")
        return f"validating ({done_features}/{len(features)} features done)"
    return phase.status


def _feature_detail(feature: MissionFeature) -> str:
    worker = feature.assigned_worker_ids[-1] if feature.assigned_worker_ids else ""
    if feature.status == "blocked":
        return "BLOCKED — see handoff_items[0]"
    if feature.status == "in_progress":
        return f"{worker} (handoff pending)" if worker else "handoff pending"
    if feature.status == "done":
        return worker or "done"
    return worker or feature.status
