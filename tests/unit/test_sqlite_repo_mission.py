from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.migration_errors import IllegalPhaseTransition
from hungerloop.repository.sqlite_repo import SQLiteRepository

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
    "count_validation_contract_summary",
]


def _repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository.open(tmp_path / "hungerloop.sqlite")


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


def _save_mission_graph(repo: SQLiteRepository, mission: Mission) -> None:
    repo.save_mission(mission)
    for phase in mission.phases:
        repo.save_mission_phase(phase)
    for feature in mission.features:
        repo.save_mission_feature(feature)


def test_repository_exposes_mission_crud_methods(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    for name in [*MISSION_METHODS, "list_features_for_phase"]:
        assert callable(getattr(repo, name))


def test_roundtrip_mission_payload_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
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

    loaded = repo.get_mission("task-1")
    assert loaded == mission

    row = repo.conn.execute(
        "SELECT task_id, payload_json FROM missions WHERE mission_id = ?",
        ("mission-1",),
    ).fetchone()
    assert row is not None
    assert str(row["task_id"]) == "task-1"
    assert Mission.model_validate_json(str(row["payload_json"])) == mission


def test_roundtrip_matches_in_memory_repo(tmp_path: Path) -> None:
    sqlite_repo = _repo(tmp_path)
    sqlite_repo.create_task("task-1", "Build mission")

    memory_repo = InMemoryRepository()
    memory_repo.create_task("task-1", "Build mission")

    mission = _make_mission(
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

    _save_mission_graph(sqlite_repo, mission)
    memory_repo.save_mission(mission)
    for phase in mission.phases:
        memory_repo.save_mission_phase(phase)
    for feature in mission.features:
        memory_repo.save_mission_feature(feature)

    sqlite_loaded = sqlite_repo.get_mission("task-1")
    memory_loaded = memory_repo.get_mission("task-1")

    assert sqlite_loaded is not None
    assert memory_loaded is not None
    assert sqlite_loaded.model_dump() == memory_loaded.model_dump()


def test_list_methods_filter_by_mission_and_phase(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
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

    assert [phase.phase_id for phase in repo.list_mission_phases("mission-1")] == [
        "phase-1",
        "phase-2",
    ]
    assert [
        feature.feature_id
        for feature in repo.list_mission_features(
            mission_id="mission-1", phase_id="phase-1"
        )
    ] == ["feature-1"]
    assert [
        feature.feature_id
        for feature in repo.list_features_for_phase("phase-3")
    ] == ["feature-3"]


@pytest.mark.parametrize("target_status", ["pending", "in_progress", "validating"])
def test_done_phase_not_revertible_at_repo_layer(
    tmp_path: Path, target_status: str
) -> None:
    repo = _repo(tmp_path)
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

    completed_at = _ts()
    repo.update_phase_status("phase-1", "done", completed_at=completed_at)

    with pytest.raises(IllegalPhaseTransition):
        repo.update_phase_status("phase-1", target_status)

    row = repo.conn.execute(
        "SELECT status, payload_json FROM mission_phases WHERE phase_id = ?",
        ("phase-1",),
    ).fetchone()
    assert row is not None
    assert str(row["status"]) == "done"
    assert MissionPhase.model_validate_json(str(row["payload_json"])).completed_at == (
        completed_at
    )


def test_blocked_feature_must_go_through_compiler(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
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

    row = repo.conn.execute(
        "SELECT status, payload_json FROM mission_features WHERE feature_id = ?",
        ("feature-1",),
    ).fetchone()
    assert row is not None
    assert str(row["status"]) == "blocked"
    assert MissionFeature.model_validate_json(str(row["payload_json"])).status == (
        "blocked"
    )


def test_save_mission_with_unknown_task_id_rolls_back(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    mission = _make_mission(
        mission_id="mission-1",
        task_id="ghost-task",
        phases=[_make_phase("phase-1", feature_ids=["feature-1"])],
        features=[
            _make_feature("feature-1", phase_id="phase-1", hunger_item_id="H-1")
        ],
    )

    with pytest.raises(sqlite3.IntegrityError):
        repo.save_mission(mission)

    count = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM missions WHERE task_id = ?",
        ("ghost-task",),
    ).fetchone()
    assert count is not None
    assert int(count["n"]) == 0


def test_assertion_update_and_contract_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
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

    row = repo.conn.execute(
        "SELECT status, payload_json FROM validation_assertions WHERE assertion_id = ?",
        ("assert-1",),
    ).fetchone()
    assert row is not None
    assert str(row["status"]) == "passed"
    updated = ValidationAssertion.model_validate_json(str(row["payload_json"]))
    assert updated.validated_at_loop == 4
    assert updated.evidence_ids == ["E-1"]


def test_count_validation_contract_summary_returns_all_status_counts(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    repo.create_task("task-1", "Build mission")
    mission = _make_mission(
        mission_id="mission-1",
        task_id="task-1",
        phases=[
            _make_phase(
                "phase-1",
                feature_ids=[],
                validation_contract_ids=[
                    "assert-pending",
                    "assert-passed",
                    "assert-failed",
                    "assert-blocked",
                    "assert-passed-2",
                ],
            )
        ],
        features=[],
    )
    _save_mission_graph(repo, mission)
    repo.save_validation_contract(
        ValidationContract(
            mission_id="mission-1",
            assertions=[
                _make_assertion("assert-pending", phase_id="phase-1"),
                _make_assertion("assert-passed", phase_id="phase-1", status="passed"),
                _make_assertion("assert-failed", phase_id="phase-1", status="failed"),
                _make_assertion(
                    "assert-blocked", phase_id="phase-1", status="blocked"
                ),
                _make_assertion(
                    "assert-passed-2", phase_id="phase-1", status="passed"
                ),
            ],
        )
    )

    assert repo.count_validation_contract_summary("mission-1") == {
        "pending": 1,
        "passed": 2,
        "failed": 1,
        "blocked": 1,
    }
    assert repo.count_validation_contract_summary("missing-mission") == {
        "pending": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
    }
