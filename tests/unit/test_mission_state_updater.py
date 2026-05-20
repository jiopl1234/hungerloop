from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services import mission_state_updater as updater_module
from hungerloop.services.mission_state_updater import (
    MissionRegenerateResult,
    MissionStateUpdater,
)
from hungerloop.services.path_safety import resolve_workspace_path

TASK_ID = "task-1"
MISSION_ID = "mission-1"
PHASE_ID = "phase-1"
ARTIFACT_NAMES = [
    "mission.md",
    "features.yaml",
    "validation-contract.yaml",
    "services.yaml",
]


def _ts() -> datetime:
    return datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _feature(
    feature_id: str,
    *,
    status: str = "pending",
    description: str | None = None,
) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=f"H-{feature_id}",
        phase_id=PHASE_ID,
        title=f"Feature {feature_id}",
        description=description or f"Description for {feature_id}",
        preconditions=["phase-ready"],
        expected_behavior=["works"],
        verification_steps=[".venv/bin/pytest -q"],
        fulfills=["VAL-M5-019"],
        status=status,
        assigned_worker_ids=["worker-1"],
    )


def _assertion(
    assertion_id: str,
    *,
    status: str = "pending",
    description: str | None = None,
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=assertion_id,
        phase_id=PHASE_ID,
        title=f"Assertion {assertion_id}",
        description=description or f"Description for {assertion_id}",
        check_type="behavioral_assertion",
        params={"file": "mission.md", "headers": ["## Description"]},
        evidence_requirements=["terminal output"],
        status=status,
        validated_at_loop=7 if status == "passed" else None,
        evidence_ids=["EV-1"] if status == "passed" else [],
    )


def _seed_repo(
    *,
    services_manifest: dict[str, Any] | None = None,
    feature_count: int = 2,
) -> tuple[InMemoryRepository, Mission, ValidationContract]:
    repo = InMemoryRepository()
    repo.create_task(TASK_ID, "Build mission mirrors")
    features = [
        _feature(
            f"F-{idx}",
            status="in_progress" if idx == 1 else "pending",
            description=(
                "Line one\nLine two" if idx == 1 else f"Description for F-{idx}"
            ),
        )
        for idx in range(1, feature_count + 1)
    ]
    assertions = [
        _assertion("VAL-1", status="passed", description="First line\nSecond line"),
        _assertion("VAL-2"),
    ]
    phase = MissionPhase(
        phase_id=PHASE_ID,
        title="Implementation",
        description="Implement state updater",
        feature_ids=[feature.feature_id for feature in features],
        validation_contract_ids=[assertion.assertion_id for assertion in assertions],
        status="in_progress",
    )
    mission = Mission(
        mission_id=MISSION_ID,
        task_id=TASK_ID,
        title="State Updater Mission",
        description="Project SQLite mission state into best artifacts.",
        phases=[phase],
        features=features,
        created_at=_ts(),
        services_manifest=services_manifest,
    )
    contract = ValidationContract(mission_id=MISSION_ID, assertions=assertions)
    repo.save_mission(mission)
    repo.save_validation_contract(contract)
    return repo, mission, contract


def _best_root(tmp_path: Path) -> Path:
    best_root = tmp_path / "best"
    best_root.mkdir()
    return best_root


def test_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)
    temp_dirs: list[Path] = []
    replace_calls: list[tuple[Path, Path]] = []
    real_named_tempfile = updater_module.tempfile.NamedTemporaryFile
    real_replace = updater_module.os.replace

    def recording_named_tempfile(*args: Any, **kwargs: Any) -> Any:
        temp_dirs.append(Path(kwargs["dir"]).resolve())
        return real_named_tempfile(*args, **kwargs)

    def recording_replace(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        replace_calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(
        updater_module.tempfile,
        "NamedTemporaryFile",
        recording_named_tempfile,
    )
    monkeypatch.setattr(updater_module.os, "replace", recording_replace)

    result = MissionStateUpdater(repo).regenerate(
        TASK_ID,
        best_workspace_root=best_root,
    )

    assert isinstance(result, MissionRegenerateResult)
    assert [path.name for path in result.artifact_paths] == ARTIFACT_NAMES
    assert [dst.name for _src, dst in replace_calls] == ARTIFACT_NAMES
    assert temp_dirs == [best_root.resolve()] * len(ARTIFACT_NAMES)
    assert all(src.parent == best_root.resolve() for src, _dst in replace_calls)
    assert all((best_root / name).exists() for name in ARTIFACT_NAMES)


def test_features_yaml_atomic_and_ordered(tmp_path: Path) -> None:
    repo, mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    content = (best_root / "features.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    expected = {
        "features": [feature.model_dump(mode="json") for feature in mission.features]
    }
    assert parsed == expected
    assert list(parsed["features"][0].keys()) == list(MissionFeature.model_fields)
    assert 'status: "in_progress"' in content
    assert "description: |" in content


def test_validation_contract_yaml(tmp_path: Path) -> None:
    repo, _mission, contract = _seed_repo()
    best_root = _best_root(tmp_path)

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    content = (best_root / "validation-contract.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    expected = {
        "assertions": [
            assertion.model_dump(mode="json") for assertion in contract.assertions
        ]
    }
    assert parsed == expected
    assert list(parsed["assertions"][0].keys()) == list(ValidationAssertion.model_fields)
    assert 'status: "passed"' in content
    assert "description: |" in content


def test_services_yaml_emits_empty_list(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    assert (best_root / "services.yaml").read_text(encoding="utf-8") == (
        "services: []\n"
    )
    assert yaml.safe_load((best_root / "services.yaml").read_text()) == {
        "services": []
    }


def test_services_yaml_mirrors_manifest_when_present(tmp_path: Path) -> None:
    services_manifest = {
        "services": {
            "api": {
                "start": "python -m api",
                "stop": "python -m api.stop",
                "healthcheck": "python -m api.health",
                "port": 8123,
                "depends_on": [],
            }
        },
        "commands": {"test": ".venv/bin/pytest -q"},
    }
    repo, _mission, _contract = _seed_repo(services_manifest=services_manifest)
    best_root = _best_root(tmp_path)

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    assert yaml.safe_load((best_root / "services.yaml").read_text()) == (
        services_manifest
    )


def test_deterministic_render(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)
    updater = MissionStateUpdater(repo)

    first_result = updater.regenerate(TASK_ID, best_workspace_root=best_root)
    first = {path.name: path.read_bytes() for path in first_result.artifact_paths}
    second_result = updater.regenerate(TASK_ID, best_workspace_root=best_root)
    second = {path.name: path.read_bytes() for path in second_result.artifact_paths}

    assert first == second


def test_emits_state_regenerated_event(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)

    result = MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    events = repo.list_events(TASK_ID, event_types=["mission.state_regenerated"])
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["mission_id"] == MISSION_ID
    assert payload["artifact_paths"] == [str(path) for path in result.artifact_paths]
    assert payload["duration_ms"] == result.duration_ms
    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 0


def test_slow_regenerate_emits_paired_event(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)
    ticks = iter([10.0, 10.101])

    MissionStateUpdater(repo, clock=lambda: next(ticks)).regenerate(
        TASK_ID,
        best_workspace_root=best_root,
    )

    events = repo.list_events(TASK_ID)
    assert [event["event_type"] for event in events] == [
        "mission.state_regenerated",
        "MISSION_STATE_REGENERATION_SLOW",
    ]
    assert events[1]["payload"]["duration_ms"] >= 101


def test_mission_md_sections(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    text = (best_root / "mission.md").read_text(encoding="utf-8")
    assert text.startswith("# State Updater Mission\n")
    for section in [
        "## Description",
        "## Phases",
        "### phase-1 Implementation",
        "## Constraints",
        "## Notes",
    ]:
        assert section in text
    assert "- [in_progress] Feature F-1" in text
    assert "- [passed] Assertion VAL-1" in text
    for invariant in ["I-3", "I-4", "I-5", "I-6", "I-7", "I-8", "I-9", "I-10"]:
        assert f"- **{invariant}" in text


def test_failure_emits_event_and_reraises(tmp_path: Path) -> None:
    repo, _mission, _contract = _seed_repo(services_manifest={"services": object()})
    best_root = _best_root(tmp_path)

    with pytest.raises(Exception):
        MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    events = repo.list_events(TASK_ID, event_types=["MISSION_STATE_REGENERATION_FAILED"])
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["mission_id"] == MISSION_ID
    assert payload["artifact_paths"] == []
    assert "error_type" in payload


def test_artifact_paths_are_resolved_through_path_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _mission, _contract = _seed_repo()
    best_root = _best_root(tmp_path)
    calls: list[tuple[Path, str]] = []

    def recording_resolve(root: Path, user_path: str) -> Path:
        calls.append((root, user_path))
        return resolve_workspace_path(root, user_path)

    monkeypatch.setattr(
        updater_module,
        "resolve_workspace_path",
        recording_resolve,
    )

    MissionStateUpdater(repo).regenerate(TASK_ID, best_workspace_root=best_root)

    assert [user_path for _root, user_path in calls] == ARTIFACT_NAMES


def test_regenerate_noops_for_legacy_task(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    repo.create_task("legacy-task", "Legacy goal")
    best_root = _best_root(tmp_path)
    updater = MissionStateUpdater(repo)

    result = updater.regenerate("legacy-task", best_workspace_root=best_root)

    assert result.artifact_paths == []
    assert result.duration_ms >= 0
    assert list(best_root.iterdir()) == []
    assert repo.list_events("legacy-task") == []
