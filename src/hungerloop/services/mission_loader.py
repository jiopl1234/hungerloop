"""Load operator-authored mission specs from Markdown/YAML (REQ-M6-001..002)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field, TypeAdapter

from hungerloop.models.mission import (
    MissionFeature,
    MissionPhase,
    MissionPhaseStatus,
)
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationContract,
)

_FEATURE_LIST_ADAPTER: TypeAdapter[list[MissionFeature]] = TypeAdapter(
    list[MissionFeature]
)
_ASSERTION_LIST_ADAPTER: TypeAdapter[list[ValidationAssertion]] = TypeAdapter(
    list[ValidationAssertion]
)

_PHASE_HEADER_RE = re.compile(r"^###\s+(?P<phase_id>\S+)(?:\s+(?P<title>.*))?$")
_STATUS_RE = re.compile(r"^Status:\s*`?(?P<status>[^`]+?)`?\s*$")
_FEATURE_BULLET_RE = re.compile(
    r"^-\s+\[[^\]]+\]\s+Feature\s+(?P<feature_id>[^:]+):"
)
_ASSERTION_BULLET_RE = re.compile(
    r"^-\s+\[[^\]]+\]\s+Assertion\s+(?P<assertion_id>[^:]+):"
)


class MissionLoadError(ValueError):
    """Raised when a mission spec file cannot be loaded before schema validation."""


class ParsedMissionSpec(BaseModel):
    """Partially parsed mission import payload.

    YAML/Markdown files are optional during import, so absent file families are
    represented as ``None``. The compiler treats ``None`` as "preserve existing
    SQLite state" and an empty list as "replace with empty".
    """

    title: str
    description: str = ""
    phases: list[MissionPhase] | None = None
    features: list[MissionFeature] | None = None
    validation_contract: ValidationContract | None = None
    services_manifest: dict[str, Any] | None = None
    source_files: list[str] = Field(default_factory=list)
    mission_markdown_loaded: bool = False


class MissionLoader:
    """Parse ``mission.md`` and mission YAML mirrors using ``yaml.safe_load``."""

    def load_from_path(self, path: Path) -> ParsedMissionSpec:
        """Load a mission spec from a directory or a single Markdown file."""
        resolved = path.resolve(strict=True)
        if resolved.is_dir():
            return self._load_directory(resolved)
        return self._load_markdown_file(resolved)

    def _load_directory(self, directory: Path) -> ParsedMissionSpec:
        title = directory.name
        description = ""
        phases: list[MissionPhase] | None = None
        features: list[MissionFeature] | None = None
        validation_contract: ValidationContract | None = None
        services_manifest: dict[str, Any] | None = None
        source_files: list[str] = []
        mission_markdown_loaded = False

        mission_path = directory / "mission.md"
        if mission_path.exists():
            title, description, phases = self._parse_markdown(mission_path)
            source_files.append(mission_path.name)
            mission_markdown_loaded = True

        features_path = directory / "features.yaml"
        if features_path.exists():
            features = self._load_features(features_path)
            source_files.append(features_path.name)

        contract_path = directory / "validation-contract.yaml"
        if contract_path.exists():
            validation_contract = self._load_validation_contract(contract_path)
            source_files.append(contract_path.name)

        services_path = directory / "services.yaml"
        if services_path.exists():
            services_manifest = self._load_services_manifest(services_path)
            source_files.append(services_path.name)

        return ParsedMissionSpec(
            title=title,
            description=description,
            phases=self._merge_phase_links(phases, features, validation_contract),
            features=features,
            validation_contract=validation_contract,
            services_manifest=services_manifest,
            source_files=sorted(source_files),
            mission_markdown_loaded=mission_markdown_loaded,
        )

    def _load_markdown_file(self, path: Path) -> ParsedMissionSpec:
        title, description, phases = self._parse_markdown(path)
        return ParsedMissionSpec(
            title=title,
            description=description,
            phases=phases,
            source_files=[path.name],
            mission_markdown_loaded=True,
        )

    def _parse_markdown(self, path: Path) -> tuple[str, str, list[MissionPhase]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        title = self._parse_title(lines, path)
        description = self._parse_description(lines)
        phases = self._parse_phases(lines)
        return title, description, phases

    @staticmethod
    def _parse_title(lines: list[str], path: Path) -> str:
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip()
                if title:
                    return title
        raise MissionLoadError(f"{path.name}: missing '# Title' header")

    def _parse_description(self, lines: list[str]) -> str:
        return "\n".join(
            self._trim_blank_lines(
                self._section_lines(lines, "## Description")
            )
        )

    def _parse_phases(self, lines: list[str]) -> list[MissionPhase]:
        phase_lines = self._section_lines(lines, "## Phases")
        phases: list[MissionPhase] = []
        current: list[str] = []
        for line in phase_lines:
            if line.startswith("### "):
                if current:
                    phases.append(self._parse_phase_block(current))
                current = [line]
            elif current:
                current.append(line)
        if current:
            phases.append(self._parse_phase_block(current))
        return phases

    @staticmethod
    def _section_lines(lines: list[str], header: str) -> list[str]:
        start: int | None = None
        for index, line in enumerate(lines):
            if line.strip() == header:
                start = index + 1
                break
        if start is None:
            return []

        end = len(lines)
        for index in range(start, len(lines)):
            line = lines[index]
            if line.startswith("## ") and not line.startswith("### "):
                end = index
                break
        return lines[start:end]

    def _parse_phase_block(self, block: list[str]) -> MissionPhase:
        header = block[0].strip()
        match = _PHASE_HEADER_RE.match(header)
        if match is None:
            raise MissionLoadError(f"Invalid phase header: {header}")
        phase_id = match.group("phase_id").strip()
        title = (match.group("title") or phase_id).strip()
        status: MissionPhaseStatus = "pending"
        description_lines: list[str] = []
        feature_ids: list[str] = []
        assertion_ids: list[str] = []
        section = "description"

        for raw_line in block[1:]:
            stripped = raw_line.strip()
            if not stripped and section == "description":
                description_lines.append("")
                continue
            status_match = _STATUS_RE.match(stripped)
            if status_match is not None:
                status = cast(MissionPhaseStatus, status_match.group("status").strip())
                continue
            if stripped == "Features:":
                section = "features"
                continue
            if stripped == "Assertions:":
                section = "assertions"
                continue
            if section == "features":
                feature_match = _FEATURE_BULLET_RE.match(stripped)
                if feature_match is not None:
                    feature_ids.append(feature_match.group("feature_id").strip())
            elif section == "assertions":
                assertion_match = _ASSERTION_BULLET_RE.match(stripped)
                if assertion_match is not None:
                    assertion_ids.append(assertion_match.group("assertion_id").strip())
            elif stripped:
                description_lines.append(raw_line.rstrip())

        return MissionPhase(
            phase_id=phase_id,
            title=title,
            description="\n".join(self._trim_blank_lines(description_lines)),
            feature_ids=list(dict.fromkeys(feature_ids)),
            validation_contract_ids=list(dict.fromkeys(assertion_ids)),
            status=status,
        )

    def _load_features(self, path: Path) -> list[MissionFeature]:
        payload = self._read_yaml(path)
        if payload is None:
            return []
        raw_features: object
        if isinstance(payload, list):
            raw_features = payload
        else:
            mapping = self._require_mapping(payload, path)
            raw_features = mapping.get("features", [])
        if raw_features is None:
            return []
        if not isinstance(raw_features, list):
            raise MissionLoadError(f"{path.name}: field 'features' must be a list")
        return _FEATURE_LIST_ADAPTER.validate_python(raw_features)

    def _load_validation_contract(self, path: Path) -> ValidationContract:
        payload = self._read_yaml(path)
        if payload is None:
            return ValidationContract(mission_id="", assertions=[])
        mission_id = ""
        raw_assertions: object
        if isinstance(payload, list):
            raw_assertions = payload
        else:
            mapping = self._require_mapping(payload, path)
            mission_id = str(mapping.get("mission_id", ""))
            raw_assertions = mapping.get("assertions", [])
        if raw_assertions is None:
            raw_assertions = []
        if not isinstance(raw_assertions, list):
            raise MissionLoadError(f"{path.name}: field 'assertions' must be a list")
        assertions = _ASSERTION_LIST_ADAPTER.validate_python(raw_assertions)
        return ValidationContract(mission_id=mission_id, assertions=assertions)

    def _load_services_manifest(self, path: Path) -> dict[str, Any]:
        payload = self._read_yaml(path)
        if payload is None:
            return {}
        return self._require_mapping(payload, path)

    @staticmethod
    def _read_yaml(path: Path) -> object:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise MissionLoadError(f"{path.name}: YAML parse error: {exc}") from exc

    @staticmethod
    def _require_mapping(payload: object, path: Path) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MissionLoadError(f"{path.name}: expected a YAML mapping")
        return cast(dict[str, Any], payload)

    @staticmethod
    def _merge_phase_links(
        phases: list[MissionPhase] | None,
        features: list[MissionFeature] | None,
        validation_contract: ValidationContract | None,
    ) -> list[MissionPhase] | None:
        if phases is None:
            return None
        assertions = validation_contract.assertions if validation_contract else None
        merged: list[MissionPhase] = []
        for phase in phases:
            feature_ids = (
                _merge_ids(
                    phase.feature_ids,
                    [
                        feature.feature_id
                        for feature in features
                        if feature.phase_id == phase.phase_id
                    ],
                )
                if features is not None
                else phase.feature_ids
            )
            assertion_ids = (
                _merge_ids(
                    phase.validation_contract_ids,
                    [
                        assertion.assertion_id
                        for assertion in assertions
                        if assertion.phase_id == phase.phase_id
                    ],
                )
                if assertions is not None
                else phase.validation_contract_ids
            )
            merged.append(
                phase.model_copy(
                    update={
                        "feature_ids": feature_ids,
                        "validation_contract_ids": assertion_ids,
                    }
                )
            )
        return merged

    @staticmethod
    def _trim_blank_lines(lines: list[str]) -> list[str]:
        start = 0
        end = len(lines)
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        return lines[start:end]


def _merge_ids(existing: list[str], desired: list[str]) -> list[str]:
    desired_set = set(desired)
    ordered = [item_id for item_id in existing if item_id in desired_set]
    ordered.extend(item_id for item_id in desired if item_id not in ordered)
    return ordered
