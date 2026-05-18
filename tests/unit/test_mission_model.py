from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from hungerloop.models import Mission, MissionFeature, MissionPhase
from hungerloop.models.mission import Mission as MissionFromModule
from hungerloop.models.mission import MissionFeature as MissionFeatureFromModule
from hungerloop.models.mission import MissionPhase as MissionPhaseFromModule
from hungerloop.repository.migration_errors import IllegalPhaseTransition


def _make_phase() -> MissionPhase:
    return MissionPhase(
        phase_id="phase-1",
        title="Phase 1",
        description="Build the first phase",
        feature_ids=["feature-1"],
        validation_contract_ids=["VAL-1"],
    )


def _make_feature() -> MissionFeature:
    return MissionFeature(
        feature_id="feature-1",
        hunger_item_id="H-1",
        phase_id="phase-1",
        title="Feature 1",
        description="Implement a feature",
        preconditions=["phase-ready"],
        expected_behavior=["It works"],
        verification_steps=["Run pytest"],
        fulfills=["VAL-1"],
    )


def test_models_are_exported_and_exception_is_importable() -> None:
    assert Mission is MissionFromModule
    assert MissionPhase is MissionPhaseFromModule
    assert MissionFeature is MissionFeatureFromModule
    assert IllegalPhaseTransition.__name__ == "IllegalPhaseTransition"


def test_mission_phase_defaults_and_docstring() -> None:
    phase = _make_phase()

    assert phase.status == "pending"
    assert phase.completed_at is None
    assert "REQ-M1-001" in (MissionPhase.__doc__ or "")

    phase.status = "in_progress"
    assert phase.status == "in_progress"


def test_mission_phase_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        MissionPhase(
            phase_id="phase-1",
            title="Phase 1",
            description="Build the first phase",
            feature_ids=[],
            validation_contract_ids=[],
            status="not_a_phase",  # type: ignore[arg-type]
        )


def test_mission_feature_defaults_and_docstring() -> None:
    feature = _make_feature()

    assert feature.status == "pending"
    assert feature.assigned_worker_ids == []
    assert "REQ-M1-002" in (MissionFeature.__doc__ or "")

    feature.status = "blocked"
    assert feature.status == "blocked"


def test_mission_feature_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        MissionFeature(
            feature_id="feature-1",
            hunger_item_id="H-1",
            phase_id="phase-1",
            title="Feature 1",
            description="Implement a feature",
            preconditions=[],
            expected_behavior=[],
            verification_steps=[],
            fulfills=[],
            status="garbage",  # type: ignore[arg-type]
        )


def test_mission_minimal_instance_reserved_fields_and_docstring() -> None:
    mission = Mission(
        mission_id="mission-1",
        task_id="task-1",
        title="Mission title",
        description="Mission description",
        phases=[_make_phase()],
        features=[_make_feature()],
        created_at=datetime(2026, 5, 18, 12, 0, 0),
    )

    assert mission.started_at is None
    assert mission.completed_at is None
    assert mission.max_parallel_features is None
    assert mission.services_manifest is None
    assert "max_parallel_features" in Mission.model_fields
    assert "services_manifest" in Mission.model_fields
    assert "REQ-M1-003" in (Mission.__doc__ or "")
