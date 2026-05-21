from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hungerloop.models.enums import AcceptanceCheckType, EvidenceType, HungerItemStatus
from hungerloop.models.hunger import HungerItem, HungerLedger
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import ValidationAssertion, ValidationContract
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.mission_loader import MissionLoader, ParsedMissionSpec
from hungerloop.services.requirement_compiler import RequirementCompiler

RepoUnderTest = InMemoryRepository | SQLiteRepository


def _ts() -> datetime:
    return datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _phase(phase_id: str, *, title: str | None = None) -> MissionPhase:
    return MissionPhase(
        phase_id=phase_id,
        title=title or f"Phase {phase_id}",
        description=f"Description for {phase_id}",
        feature_ids=[],
        validation_contract_ids=[],
    )


def _feature(
    feature_id: str,
    phase_id: str,
    hunger_item_id: str,
    *,
    title: str | None = None,
    status: str = "pending",
) -> MissionFeature:
    return MissionFeature(
        feature_id=feature_id,
        hunger_item_id=hunger_item_id,
        phase_id=phase_id,
        title=title or f"Feature {feature_id}",
        description=f"Implement {feature_id}",
        preconditions=["bootstrap"],
        expected_behavior=[f"{feature_id} works"],
        verification_steps=[".venv/bin/pytest -q"],
        fulfills=[f"VAL-{feature_id}"],
        status=status,
    )


def _assertion(
    assertion_id: str,
    phase_id: str,
    *,
    title: str | None = None,
    status: str = "pending",
) -> ValidationAssertion:
    return ValidationAssertion(
        assertion_id=assertion_id,
        phase_id=phase_id,
        title=title or f"Assertion {assertion_id}",
        description=f"Verify {assertion_id}",
        check_type="behavioral_assertion",
        params={"assertion": assertion_id},
        evidence_requirements=["terminal output"],
        status=status,
    )


def _write_mission_dir(tmp_path: Path, *, include_services: bool = True) -> Path:
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    (mission_dir / "mission.md").write_text(
        "\n".join(
            [
                "# Demo Mission",
                "",
                "## Description",
                "",
                "Build the demo mission.",
                "",
                "## Phases",
                "",
                "### phase-1 Bootstrap",
                "",
                "Status: `in_progress`",
                "",
                "Prepare the runtime.",
                "",
                "Features:",
                "- [pending] Feature feature-1: Loader (hunger: H-001)",
                "",
                "Assertions:",
                "- [pending] Assertion VAL-001: Loader happy path (behavioral_assertion)",
                "",
                "## Notes",
                "",
                "- none",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (mission_dir / "features.yaml").write_text(
        yaml.safe_dump(
            {
                "features": [
                    _feature(
                        "feature-1",
                        "phase-1",
                        "H-001",
                        title="Loader",
                    ).model_dump(mode="json")
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (mission_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    _assertion(
                        "VAL-001",
                        "phase-1",
                        title="Loader happy path",
                    ).model_dump(mode="json")
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if include_services:
        (mission_dir / "services.yaml").write_text(
            yaml.safe_dump(
                {"commands": {"test": ".venv/bin/pytest -q"}, "services": {}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    return mission_dir


def test_load_from_directory_parses_markdown_yaml_and_services(tmp_path: Path) -> None:
    parsed = MissionLoader().load_from_path(_write_mission_dir(tmp_path))

    assert parsed.title == "Demo Mission"
    assert parsed.description == "Build the demo mission."
    assert parsed.phases is not None
    assert [(phase.phase_id, phase.title, phase.status) for phase in parsed.phases] == [
        ("phase-1", "Bootstrap", "in_progress")
    ]
    assert parsed.features is not None
    assert [feature.feature_id for feature in parsed.features] == ["feature-1"]
    assert parsed.validation_contract is not None
    assert [
        assertion.assertion_id
        for assertion in parsed.validation_contract.assertions
    ] == ["VAL-001"]
    assert parsed.services_manifest == {
        "commands": {"test": ".venv/bin/pytest -q"},
        "services": {},
    }
    assert parsed.source_files == [
        "features.yaml",
        "mission.md",
        "services.yaml",
        "validation-contract.yaml",
    ]


def test_load_from_directory_skips_missing_optional_services_yaml(
    tmp_path: Path,
) -> None:
    parsed = MissionLoader().load_from_path(
        _write_mission_dir(tmp_path, include_services=False)
    )

    assert parsed.services_manifest is None
    assert parsed.features is not None
    assert parsed.validation_contract is not None


def test_load_from_path_accepts_single_mission_markdown(tmp_path: Path) -> None:
    mission_dir = _write_mission_dir(tmp_path)
    parsed = MissionLoader().load_from_path(mission_dir / "mission.md")

    assert parsed.title == "Demo Mission"
    assert parsed.phases is not None
    assert [phase.phase_id for phase in parsed.phases] == ["phase-1"]
    assert parsed.features is None
    assert parsed.validation_contract is None
    assert parsed.source_files == ["mission.md"]


def test_validation_contract_schema_error_names_offending_field(
    tmp_path: Path,
) -> None:
    mission_dir = _write_mission_dir(tmp_path)
    (mission_dir / "validation-contract.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {
                        "phase_id": "phase-1",
                        "title": "Missing id",
                        "description": "No assertion_id field",
                        "check_type": "behavioral_assertion",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        MissionLoader().load_from_path(mission_dir)

    assert "assertion_id" in str(excinfo.value)


@pytest.fixture(params=["in_memory", "sqlite"], ids=["in_memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[RepoUnderTest]:
    if request.param == "sqlite":
        repository: RepoUnderTest = SQLiteRepository.open(tmp_path / "hungerloop.sqlite")
    else:
        repository = InMemoryRepository()
    repository.create_task("task-1", "Original mission")
    yield repository
    if isinstance(repository, SQLiteRepository):
        repository.close()


def _seed_existing_mission(repo: RepoUnderTest) -> None:
    mission = Mission(
        mission_id="mission-task-1",
        task_id="task-1",
        title="Original Mission",
        description="Original description",
        phases=[
            MissionPhase(
                phase_id="phase-1",
                title="Original Phase",
                description="Original phase",
                feature_ids=["feature-keep", "feature-remove"],
                validation_contract_ids=["VAL-KEEP", "VAL-REMOVE"],
                status="in_progress",
            ),
            MissionPhase(
                phase_id="phase-remove",
                title="Removed Phase",
                description="Removed phase",
            ),
        ],
        features=[
            _feature(
                "feature-keep",
                "phase-1",
                "H-KEEP",
                title="Old keep title",
                status="in_progress",
            ),
            _feature("feature-remove", "phase-1", "H-REMOVE"),
        ],
        created_at=_ts(),
        services_manifest={"commands": {"test": "old"}},
    )
    repo.save_mission(mission)
    repo.save_validation_contract(
        ValidationContract(
            mission_id=mission.mission_id,
            assertions=[
                _assertion(
                    "VAL-KEEP",
                    "phase-1",
                    title="Old assertion title",
                    status="passed",
                ),
                _assertion("VAL-REMOVE", "phase-1"),
            ],
        )
    )
    repo.save_hunger_ledger(
        "task-1",
        HungerLedger(
            task_id="task-1",
            items=[
                HungerItem(
                    id="H-KEEP",
                    title="Old keep title",
                    status=HungerItemStatus.BLOCKED,
                    evidence_ids=["EV-OLD"],
                ),
                HungerItem(id="H-REMOVE", title="Removed feature"),
                HungerItem(
                    id="H-MANUAL",
                    title="Manual follow-up",
                    generated_by="manual-import",
                ),
            ],
        ),
    )


def test_compile_mission_changes_persists_diff_and_import_evidence(
    repo: RepoUnderTest,
) -> None:
    _seed_existing_mission(repo)
    parsed = ParsedMissionSpec(
        title="Updated Mission",
        description="Updated description",
        phases=[
            MissionPhase(
                phase_id="phase-1",
                title="Updated Phase",
                description="Updated phase",
            ),
            MissionPhase(
                phase_id="phase-new",
                title="New Phase",
                description="New phase",
            ),
        ],
        features=[
            _feature("feature-keep", "phase-1", "H-KEEP", title="New keep title"),
            _feature("feature-new", "phase-new", "H-NEW", title="New feature"),
        ],
        validation_contract=ValidationContract(
            mission_id="ignored-by-compiler",
            assertions=[
                _assertion("VAL-KEEP", "phase-1", title="New assertion title"),
                _assertion("VAL-NEW", "phase-new", title="New assertion"),
            ],
        ),
        services_manifest={"commands": {"test": "new"}},
        source_files=["mission.md", "features.yaml", "validation-contract.yaml"],
    )

    result = RequirementCompiler(repo).compile_mission_changes("task-1", parsed)

    assert result.evidence_id
    operation_keys = {
        (operation.op, operation.entity_type, operation.entity_id)
        for operation in result.operations
    }
    assert ("update", "mission", "mission-task-1") in operation_keys
    assert ("update", "phase", "phase-1") in operation_keys
    assert ("remove", "phase", "phase-remove") in operation_keys
    assert ("update", "feature", "feature-keep") in operation_keys
    assert ("add", "feature", "feature-new") in operation_keys
    assert ("remove", "feature", "feature-remove") in operation_keys
    assert ("update", "assertion", "VAL-KEEP") in operation_keys
    assert ("add", "assertion", "VAL-NEW") in operation_keys
    assert ("remove", "assertion", "VAL-REMOVE") in operation_keys

    mission = repo.get_mission("task-1")
    assert mission is not None
    assert mission.title == "Updated Mission"
    assert mission.services_manifest == {"commands": {"test": "new"}}
    assert [feature.feature_id for feature in mission.features] == [
        "feature-keep",
        "feature-new",
    ]
    assert mission.features[0].status == "in_progress"
    assert mission.phases[0].feature_ids == ["feature-keep"]
    assert mission.phases[0].validation_contract_ids == ["VAL-KEEP"]

    contract = repo.get_validation_contract("mission-task-1")
    assert contract is not None
    assert [assertion.assertion_id for assertion in contract.assertions] == [
        "VAL-KEEP",
        "VAL-NEW",
    ]
    assert contract.assertions[0].status == "passed"

    ledger = repo.get_hunger_ledger("task-1")
    item_by_id = {item.id: item for item in ledger.items}
    assert list(item_by_id) == ["H-KEEP", "H-MANUAL", "H-NEW"]
    assert item_by_id["H-KEEP"].title == "New keep title"
    assert item_by_id["H-KEEP"].status == HungerItemStatus.BLOCKED
    assert item_by_id["H-KEEP"].evidence_ids == ["EV-OLD"]
    assert item_by_id["H-NEW"].acceptance_checks[0].check_type == (
        AcceptanceCheckType.EVIDENCE_COUNT_MIN
    )
    assert result.summary["features_added"] == 1
    assert result.summary["assertions_added"] == 1
    assert repo.count_evidence_by_type(
        "task-1",
        [result.evidence_id],
        EvidenceType.HUMAN_INPUT,
        successful_only=True,
    ) == 1

    if isinstance(repo, InMemoryRepository):
        evidence = repo._evidence[result.evidence_id]
        assert evidence["kind"] == "mission_import"
        assert evidence["summary"] == result.summary
        assert evidence["changes"] == [
            json.loads(operation.model_dump_json()) for operation in result.operations
        ]
