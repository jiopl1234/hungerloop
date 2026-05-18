from __future__ import annotations

from datetime import datetime

import pytest

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.services.requirement_compiler import RequirementCompiler


def _make_phase(phase_id: str, feature_ids: list[str]) -> MissionPhase:
    return MissionPhase(
        phase_id=phase_id,
        title=f"Phase {phase_id}",
        description=f"Description for {phase_id}",
        feature_ids=feature_ids,
        validation_contract_ids=[],
    )


def _make_feature(feature_id: str, phase_id: str, hunger_item_id: str) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id=phase_id,
        title=f"Feature {feature_id}",
        description=f"Implement {feature_id}",
        preconditions=[],
        expected_behavior=[],
        verification_steps=[],
        fulfills=[],
    )


def test_compile_features_to_ledger() -> None:
    mission = Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission 1",
        description="Compile features into hunger items",
        phases=[_make_phase("phase-1", ["feature-1", "feature-2", "feature-3"])],
        features=[
            _make_feature("feature-1", "phase-1", "H-101"),
            _make_feature("feature-2", "phase-1", "H-102"),
            _make_feature("feature-3", "phase-1", "H-103"),
        ],
        created_at=datetime(2026, 5, 18, 12, 0, 0),
    )

    ledger = RequirementCompiler().compile_mission_features("task-1", mission)

    assert ledger.task_id == "task-1"
    assert [item.id for item in ledger.items] == ["H-101", "H-102", "H-103"]
    for item in ledger.items:
        assert item.gap_score == 1.0
        assert item.priority == pytest.approx(1 / 3)
        assert item.refinement_tier == 0
        assert item.acceptance_mode == "all"
        assert len(item.acceptance_checks) == 1
        check = item.acceptance_checks[0]
        assert check.check_type == AcceptanceCheckType.EVIDENCE_COUNT_MIN
        assert check.params == {"evidence_type": "any", "min_count": 1}


def test_compile_mission_features_uses_phase_local_priority_without_mutating_mission() -> None:
    features = [
        _make_feature("feature-1", "phase-1", "H-101"),
        _make_feature("feature-2", "phase-1", "H-102"),
        _make_feature("feature-3", "phase-2", "H-201"),
    ]
    mission = Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission 1",
        description="Compile features into hunger items",
        phases=[
            _make_phase("phase-1", ["feature-1", "feature-2"]),
            _make_phase("phase-2", ["feature-3"]),
        ],
        features=features,
        created_at=datetime(2026, 5, 18, 12, 0, 0),
    )
    original_list = mission.features
    original_dumps = [feature.model_dump() for feature in mission.features]

    ledger = RequirementCompiler().compile_mission_features("task-1", mission)

    assert mission.features is original_list
    assert [feature.model_dump() for feature in mission.features] == original_dumps
    item_by_id = {item.id: item for item in ledger.items}
    assert item_by_id["H-101"].priority == pytest.approx(0.5)
    assert item_by_id["H-102"].priority == pytest.approx(0.5)
    assert item_by_id["H-201"].priority == pytest.approx(1.0)
