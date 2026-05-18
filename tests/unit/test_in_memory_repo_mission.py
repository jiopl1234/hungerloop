from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.migration_errors import IllegalPhaseTransition

MISSION_METHODS = [
    "save_mission",
    "get_mission",
    "save_mission_phase",
    "update_phase_status",
    "list_mission_phases",
    "save_mission_feature",
    "update_feature_status",
    "list_mission_features",
    "save_validation_contract",
    "get_validation_contract",
    "save_validation_assertion",
    "update_assertion_status",
    "list_validation_assertions",
]


def _ts() -> datetime:
    return datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)


def _make_phase(
    phase_id: str,
    *,
    feature_ids: list[str],
    validation_contract_ids: list[str] | None = None,
    status: str = "pending",
    completed_at: datetime | None = None,
) -> MissionPhase:
    return MissionPhase(
        phase_id=phase_id,
        title=f"Phase {phase_id}",
        description=f"Description for {phase_id}",
        feature_ids=feature_ids,
        validation_contract_ids=validation_contract_ids or [],
        status=status,
        completed_at=completed_at,
    )


def _make_feature(
    feature_id: str,
    *,
    phase_id: str,
    hunger_item_id: str,
    status: str = "pending",
) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id=phase_id,
        title=f"Feature {feature_id}",
        description=f"Description for {feature_id}",
        preconditions=["phase-ready"],
        expected_behavior=["works"],
        verification_steps=["pytest -q"],
        fulfills=["VAL-1"],
        status=status,
    )


def _make_mission(
    *,
    mission_id: str,
    task_id: str,
    phases: list[MissionPhase],
    features: list[MissionFeature],
) -> Mission:
    return Mission(
        mission_id=mission_id,
        task_id=task_id,
        title=f"Mission {mission_id}",
        description=f"Description for {mission_id}",
        phases=phases,
        features=features,
        created_at=_ts(),
    )


def _make_assertion(
    assertion_id: str,
    *,
    phase_id: str,
    status: str = "pending",
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=assertion_id,
        phase_id=phase_id,
        title=f"Assertion {assertion_id}",
        description=f"Description for {assertion_id}",
        check_type="behavioral_assertion",
        params={"needle": assertion_id},
        evidence_requirements=["terminal output"],
        status=status,
    )


def _save_mission_graph(repo: InMemoryRepository, mission: Mission) -> None:
    repo.save_mission(mission)
    for phase in mission.phases:
        repo.save_mission_phase(phase)
    for feature in mission.features:
        repo.save_mission_feature(feature)


def test_repository_exposes_mission_crud_methods() -> None:
    repo = InMemoryRepository()

    for name in [*MISSION_METHODS, "list_features_for_phase"]:
        assert callable(getattr(repo, name))


def test_roundtrip_mission_and_list_filters() -> None:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Build mission one")
    repo.create_task("task-2", "Build mission two")

    mission_one = _make_mission(
        mission_id="mission-1",
        task_id="task-1",
        phases=[
            _make_phase("phase-1", feature_ids=["feature-1"]),
            _make_phase("phase-2", feature_ids=["feature-2"]),
        ],
        features=[
            _make_feature("feature-1", phase_id="phase-1", hunger_item_id="H-1"),
            _make_feature("feature-2", phase_id="phase-2", hunger_item_id="H-2"),
        ],
    )
    mission_two = _make_mission(
        mission_id="mission-2",
        task_id="task-2",
        phases=[_make_phase("phase-3", feature_ids=["feature-3"])],
        features=[
            _make_feature("feature-3", phase_id="phase-3", hunger_item_id="H-3")
        ],
    )

    _save_mission_graph(repo, mission_one)
    _save_mission_graph(repo, mission_two)

    assert repo.get_mission("task-1") == mission_one
    assert [phase.phase_id for phase in repo.list_mission_phases("mission-1")] == [
        "phase-1",
        "phase-2",
    ]
    assert [
        feature.feature_id
        for feature in repo.list_mission_features(mission_id="mission-1")
    ] == ["feature-1", "feature-2"]
    assert [
        feature.feature_id
        for feature in repo.list_mission_features(
            mission_id="mission-1", phase_id="phase-1"
        )
    ] == ["feature-1"]
    assert [
        feature.feature_id
        for feature in repo.list_features_for_phase("phase-2")
    ] == ["feature-2"]


@pytest.mark.parametrize("target_status", ["pending", "in_progress", "validating"])
def test_done_phase_not_revertible_at_repo_layer(target_status: str) -> None:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Build mission")

    phase = _make_phase("phase-1", feature_ids=["feature-1"])
    mission = _make_mission(
        mission_id="mission-1",
        task_id="task-1",
        phases=[phase],
        features=[
            _make_feature("feature-1", phase_id="phase-1", hunger_item_id="H-1")
        ],
    )
    _save_mission_graph(repo, mission)

    completed_at = _ts()
    repo.update_phase_status("phase-1", "done", completed_at=completed_at)

    with pytest.raises(IllegalPhaseTransition):
        repo.update_phase_status("phase-1", target_status)

    stored_phase = repo.list_mission_phases("mission-1")[0]
    assert stored_phase.status == "done"
    assert stored_phase.completed_at == completed_at


def test_blocked_feature_must_go_through_compiler() -> None:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Build mission")

    mission = _make_mission(
        mission_id="mission-1",
        task_id="task-1",
        phases=[_make_phase("phase-1", feature_ids=["feature-1"])],
        features=[
            _make_feature("feature-1", phase_id="phase-1", hunger_item_id="H-1")
        ],
    )
    _save_mission_graph(repo, mission)

    repo.update_feature_status("feature-1", "blocked")

    with pytest.raises(IllegalPhaseTransition):
        repo.update_feature_status("feature-1", "done")

    stored_feature = repo.list_mission_features(
        mission_id="mission-1", phase_id="phase-1"
    )[0]
    assert stored_feature.status == "blocked"


def test_assertion_update_and_contract_roundtrip() -> None:
    repo = InMemoryRepository()
    repo.create_task("task-1", "Build mission")

    mission = _make_mission(
        mission_id="mission-1",
        task_id="task-1",
        phases=[
            _make_phase(
                "phase-1",
                feature_ids=["feature-1"],
                validation_contract_ids=["assert-1", "assert-2"],
            ),
            _make_phase(
                "phase-2",
                feature_ids=["feature-2"],
                validation_contract_ids=["assert-3"],
            ),
        ],
        features=[
            _make_feature("feature-1", phase_id="phase-1", hunger_item_id="H-1"),
            _make_feature("feature-2", phase_id="phase-2", hunger_item_id="H-2"),
        ],
    )
    _save_mission_graph(repo, mission)

    contract = ValidationContract(
        mission_id="mission-1",
        assertions=[
            _make_assertion("assert-1", phase_id="phase-1"),
            _make_assertion("assert-2", phase_id="phase-1"),
            _make_assertion("assert-3", phase_id="phase-2"),
        ],
    )
    repo.save_validation_contract(contract)
    repo.save_validation_assertion(_make_assertion("assert-4", phase_id="phase-1"))
    repo.update_assertion_status(
        "assert-1",
        "passed",
        validated_at_loop=4,
        evidence_ids=["E-1"],
    )

    loaded_contract = repo.get_validation_contract("mission-1")
    assert loaded_contract is not None
    assert {assertion.assertion_id for assertion in loaded_contract.assertions} == {
        "assert-1",
        "assert-2",
        "assert-3",
        "assert-4",
    }

    phase_one_assertions = repo.list_validation_assertions(
        mission_id="mission-1", phase_id="phase-1"
    )
    assert [assertion.assertion_id for assertion in phase_one_assertions] == [
        "assert-1",
        "assert-2",
        "assert-4",
    ]
    updated = next(
        assertion
        for assertion in repo.list_validation_assertions(mission_id="mission-1")
        if assertion.assertion_id == "assert-1"
    )
    assert updated.status == "passed"
    assert updated.validated_at_loop == 4
    assert updated.evidence_ids == ["E-1"]
