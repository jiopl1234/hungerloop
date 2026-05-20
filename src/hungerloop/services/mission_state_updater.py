"""Regenerate mission mirror artifacts from repository state (REQ-M5-030)."""
from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from pathlib import Path

import yaml

from hungerloop.models.events import EventType
from hungerloop.models.mission import Mission, MissionFeature, MissionPhase
from hungerloop.models.validation_contract import (
    ValidationAssertion,
    ValidationContract,
)
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.path_safety import resolve_workspace_path

_ARTIFACT_NAMES = [
    "mission.md",
    "features.yaml",
    "validation-contract.yaml",
    "services.yaml",
]
_SLOW_THRESHOLD_MS = 100


class _LiteralString(str):
    """String marker rendered with YAML block scalar style."""


class _QuotedString(str):
    """String marker rendered with YAML double-quoted scalar style."""


def _represent_literal_string(
    dumper: yaml.SafeDumper,
    data: _LiteralString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


def _represent_quoted_string(
    dumper: yaml.SafeDumper,
    data: _QuotedString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


yaml.SafeDumper.add_representer(_LiteralString, _represent_literal_string)
yaml.SafeDumper.add_representer(_QuotedString, _represent_quoted_string)


@dataclass(frozen=True)
class MissionRegenerateResult:
    """Result shape returned by :meth:`MissionStateUpdater.regenerate`."""

    artifact_paths: list[Path]
    duration_ms: int


class MissionStateUpdater:
    """Project SQLite mission truth into best workspace artifacts."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        *,
        slow_threshold_ms: int = _SLOW_THRESHOLD_MS,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.repo = repo
        self._slow_threshold_ms = slow_threshold_ms
        self._clock = clock

    def regenerate(
        self,
        task_id: str,
        *,
        best_workspace_root: Path,
    ) -> MissionRegenerateResult:
        """Regenerate mission mirror files under ``best_workspace_root``.

        The repository is the single source of truth: all artifact content is
        derived from :meth:`RepositoryProtocol.get_mission` and
        :meth:`RepositoryProtocol.get_validation_contract`.
        """
        start = self._clock()
        mission = self.repo.get_mission(task_id)
        if mission is None:
            return MissionRegenerateResult(
                artifact_paths=[],
                duration_ms=self._duration_ms(start),
            )

        written_paths: list[Path] = []
        try:
            root = best_workspace_root.resolve()
            contract = self.repo.get_validation_contract(
                mission.mission_id
            ) or ValidationContract(mission_id=mission.mission_id)
            artifact_texts = self._render_artifacts(mission, contract)
            artifact_paths = [
                resolve_workspace_path(root, name) for name in _ARTIFACT_NAMES
            ]

            for path in artifact_paths:
                self._write_atomic(
                    root=root,
                    target=path,
                    content=artifact_texts[path.name],
                )
                written_paths.append(path)

            duration_ms = self._duration_ms(start)
            result = MissionRegenerateResult(
                artifact_paths=artifact_paths,
                duration_ms=duration_ms,
            )
            payload = self._event_payload(
                mission=mission,
                artifact_paths=artifact_paths,
                duration_ms=duration_ms,
            )
            self.repo.append_event(
                EventType.MISSION_STATE_REGENERATED,
                payload,
                task_id=task_id,
            )
            if duration_ms > self._slow_threshold_ms:
                self.repo.append_event(
                    EventType.MISSION_STATE_REGENERATION_SLOW,
                    payload,
                    task_id=task_id,
                )
            return result
        except Exception as exc:
            duration_ms = self._duration_ms(start)
            self.repo.append_event(
                EventType.MISSION_STATE_REGENERATION_FAILED,
                {
                    "mission_id": mission.mission_id,
                    "artifact_paths": [str(path) for path in written_paths],
                    "duration_ms": duration_ms,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                task_id=task_id,
            )
            raise

    def _render_artifacts(
        self,
        mission: Mission,
        contract: ValidationContract,
    ) -> dict[str, str]:
        return {
            "mission.md": self._render_markdown(mission, contract),
            "features.yaml": self._dump_yaml(
                {
                    "features": [
                        feature.model_dump(mode="json") for feature in mission.features
                    ]
                }
            ),
            "validation-contract.yaml": self._dump_yaml(
                {
                    "assertions": [
                        assertion.model_dump(mode="json")
                        for assertion in contract.assertions
                    ]
                }
            ),
            "services.yaml": self._dump_yaml(
                mission.services_manifest
                if mission.services_manifest is not None
                else {"services": []}
            ),
        }

    def _render_markdown(
        self,
        mission: Mission,
        contract: ValidationContract,
    ) -> str:
        lines = [
            f"# {mission.title}",
            "",
            "## Description",
            "",
            mission.description,
            "",
            "## Phases",
            "",
        ]
        for phase in mission.phases:
            lines.extend(self._render_phase(phase, mission.features, contract))
        lines.extend(
            [
                "## Constraints",
                "",
                "- **I-3** Check-level commits only; score-based commits are forbidden.",
                "- **I-4** Workspace isolation: workers read best and write loop workspaces.",
                "- **I-5** Targeted validation preserves previous passing checks.",
                "- **I-6** Stagnation detection counts attempted items only.",
                "- **I-7** Sandbox and path safety guard subprocess and file access.",
                "- **I-8** Cost ceiling checks run before and after LLM/tool work.",
                "- **I-9** BLOCKED is distinct from DONE and has its own stop reason.",
                "- **I-10** Requirements are compiled by rule-based compilers.",
                "",
                "## Notes",
                "",
            ]
        )
        notes = self._operator_notes(mission)
        if notes:
            lines.extend(f"- {note}" for note in notes)
        else:
            lines.append("- None")
        lines.append("")
        return "\n".join(lines)

    def _render_phase(
        self,
        phase: MissionPhase,
        features: list[MissionFeature],
        contract: ValidationContract,
    ) -> list[str]:
        phase_features = self._ordered_features_for_phase(phase, features)
        phase_assertions = self._ordered_assertions_for_phase(phase, contract)
        lines = [
            f"### {phase.phase_id} {phase.title}",
            "",
            f"Status: `{phase.status}`",
            "",
            phase.description,
            "",
            "Features:",
        ]
        if phase_features:
            lines.extend(
                (
                    f"- [{feature.status}] Feature {feature.feature_id}: "
                    f"{feature.title} (hunger: {feature.hunger_item_id})"
                )
                for feature in phase_features
            )
        else:
            lines.append("- None")
        lines.append("")
        lines.append("Assertions:")
        if phase_assertions:
            lines.extend(
                (
                    f"- [{assertion.status}] Assertion {assertion.assertion_id}: "
                    f"{assertion.title} ({assertion.check_type})"
                )
                for assertion in phase_assertions
            )
        else:
            lines.append("- None")
        lines.extend(["", ""])
        return lines

    @staticmethod
    def _ordered_features_for_phase(
        phase: MissionPhase,
        features: list[MissionFeature],
    ) -> list[MissionFeature]:
        feature_index = {
            feature_id: index for index, feature_id in enumerate(phase.feature_ids)
        }
        return sorted(
            [feature for feature in features if feature.phase_id == phase.phase_id],
            key=lambda feature: (
                feature_index.get(feature.feature_id, len(feature_index)),
                feature.feature_id,
            ),
        )

    @staticmethod
    def _ordered_assertions_for_phase(
        phase: MissionPhase,
        contract: ValidationContract,
    ) -> list[ValidationAssertion]:
        assertion_index = {
            assertion_id: index
            for index, assertion_id in enumerate(phase.validation_contract_ids)
        }
        return sorted(
            contract.assertions_by_phase(phase.phase_id),
            key=lambda assertion: (
                assertion_index.get(
                    assertion.assertion_id,
                    len(assertion_index),
                ),
                assertion.assertion_id,
            ),
        )

    @staticmethod
    def _operator_notes(mission: Mission) -> list[str]:
        notes = getattr(mission, "notes", [])
        if isinstance(notes, list) and all(isinstance(note, str) for note in notes):
            return notes
        return []

    @staticmethod
    def _dump_yaml(payload: object) -> str:
        prepared = _prepare_yaml_value(payload)
        dumped = yaml.safe_dump(
            prepared,
            allow_unicode=True,
            default_flow_style=False,
            indent=2,
            sort_keys=False,
        )
        return _indent_top_level_sequences(dumped)

    @staticmethod
    def _write_atomic(
        *,
        root: Path,
        target: Path,
        content: str,
    ) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=root,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _duration_ms(self, start: float) -> int:
        return max(0, ceil((self._clock() - start) * 1000))

    @staticmethod
    def _event_payload(
        *,
        mission: Mission,
        artifact_paths: list[Path],
        duration_ms: int,
    ) -> dict[str, object]:
        return {
            "mission_id": mission.mission_id,
            "artifact_paths": [str(path) for path in artifact_paths],
            "duration_ms": duration_ms,
        }


def _prepare_yaml_value(value: object, *, key: str | None = None) -> object:
    if isinstance(value, dict):
        return {
            str(item_key): _prepare_yaml_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_prepare_yaml_value(item) for item in value]
    if isinstance(value, str):
        if "\n" in value:
            return _LiteralString(value)
        if key == "status":
            return _QuotedString(value)
    return value


def _indent_top_level_sequences(dumped: str) -> str:
    """Make root-key list blocks render as ``key:\n  - item``."""
    lines = dumped.splitlines()
    indented: list[str] = []
    in_root_sequence = False
    for line in lines:
        if line.startswith("- "):
            in_root_sequence = True
            indented.append(f"  {line}")
        elif in_root_sequence and line.startswith("  "):
            indented.append(f"  {line}")
        else:
            in_root_sequence = False
            indented.append(line)
    return "\n".join(indented) + ("\n" if dumped.endswith("\n") else "")


__all__ = ["MissionRegenerateResult", "MissionStateUpdater"]
