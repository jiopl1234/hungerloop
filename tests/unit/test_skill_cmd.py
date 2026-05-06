"""``hungerloop skill`` CLI tests (PRD §18 / E1-08..12).

Covers approve refusal branches, reject mandatory --reason,
export round-trip via YAML, import schema validation + fresh
skill_id generation, and the show/list disambiguation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.models.skill import ActiveSkillCard, SkillCardCandidate
from hungerloop.repository.in_memory_repo import InMemoryRepository


@pytest.fixture
def context(tmp_path: Path) -> CliContext:
    return CliContext(repo=InMemoryRepository(), workspace_root=tmp_path)


def _seed_candidate(
    repo: InMemoryRepository,
    *,
    skill_candidate_id: str = "skill-cand-1",
    state: str = "candidate",
) -> SkillCardCandidate:
    repo.create_task("t1", "Goal")
    cand = SkillCardCandidate(
        skill_candidate_id=skill_candidate_id,
        task_id="t1",
        source_best_state_id="best-1",
        name="Test skill",
        description="Test description",
        trigger_signals=["check:H-001"],
        steps=["step a", "step b"],
        accepted_check_keys=["H-001:0", "H-002:0"],
        evidence_ids=["ev-1"],
        state=state,  # type: ignore[arg-type]
        created_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    repo.save_skill_card_candidate(cand)
    return cand


# ---------------------------------------------------------------------------
# skill approve
# ---------------------------------------------------------------------------


def test_approve_promotes_candidate_to_active(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["skill", "approve", "skill-cand-1", "--activated-by", "alice"],
        obj=context,
    )
    assert result.exit_code == 0, result.output

    actives = context.repo.list_active_skill_cards()
    assert len(actives) == 1
    assert actives[0].source_candidate_id == "skill-cand-1"
    assert actives[0].activated_by == "alice"
    assert actives[0].state == "active"

    cand = context.repo.get_skill_card_candidate("skill-cand-1")
    assert cand is not None and cand.state == "active"

    types = {ev["event_type"] for ev in context.repo.list_events("t1")}
    assert "skill_card_activated" in types


def test_approve_unknown_id_is_usage_error(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "approve", "missing"], obj=context)
    assert result.exit_code != 0
    assert "not found" in result.output


def test_approve_refuses_non_candidate_state(context: CliContext) -> None:
    _seed_candidate(context.repo, state="rejected")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["skill", "approve", "skill-cand-1"], obj=context
    )
    assert result.exit_code == 2
    assert "state is" in result.output


# ---------------------------------------------------------------------------
# skill reject
# ---------------------------------------------------------------------------


def test_reject_persists_event_and_state(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["skill", "reject", "skill-cand-1", "--reason", "duplicate"],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    cand = context.repo.get_skill_card_candidate("skill-cand-1")
    assert cand is not None and cand.state == "rejected"
    types = {ev["event_type"] for ev in context.repo.list_events("t1")}
    assert "skill_card_rejected" in types


def test_reject_missing_reason_usage_error(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["skill", "reject", "skill-cand-1"], obj=context
    )
    assert result.exit_code == 2
    assert "--reason" in result.output


# ---------------------------------------------------------------------------
# skill export / import
# ---------------------------------------------------------------------------


def test_export_round_trips_via_yaml_safe_load(
    context: CliContext, tmp_path: Path
) -> None:
    repo = context.repo
    _seed_candidate(repo)
    runner = CliRunner()
    runner.invoke(cli, ["skill", "approve", "skill-cand-1"], obj=context)
    actives = repo.list_active_skill_cards()
    skill_id = actives[0].skill_id

    yaml_path = tmp_path / "skill.yaml"
    result = runner.invoke(
        cli,
        ["skill", "export", skill_id, "--output", str(yaml_path)],
        obj=context,
    )
    assert result.exit_code == 0, result.output
    assert yaml_path.exists()

    payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert payload["skill_id"] == skill_id
    assert payload["accepted_check_keys"] == ["H-001:0", "H-002:0"]
    # Stable sort_keys=True means top-level keys appear in alpha order
    # in the file.
    text = yaml_path.read_text(encoding="utf-8")
    keys_in_order = [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if ":" in line and not line.startswith("- ")
    ]
    assert keys_in_order == sorted(keys_in_order)


def test_import_generates_fresh_skill_id_and_imported_marker(
    context: CliContext, tmp_path: Path
) -> None:
    yaml_path = tmp_path / "incoming.yaml"
    payload = {
        "schema_version": "1",
        "skill_id": "SKILL-original",
        "name": "Imported skill",
        "description": "via export",
        "trigger_signals": ["check:H-001"],
        "preconditions": [],
        "steps": ["s1"],
        "tools_used": ["bash"],
        "accepted_check_keys": ["H-001:0"],
        "artifact_ids": [],
        "evidence_ids": [],
        "known_failures": [],
        "reuse_notes": [],
        "activated_at": "2026-05-04T12:00:00Z",
        "activated_by": "bob",
    }
    yaml_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "import", str(yaml_path)], obj=context)
    assert result.exit_code == 0, result.output

    actives = context.repo.list_active_skill_cards()
    assert len(actives) == 1
    new = actives[0]
    assert new.skill_id != "SKILL-original"
    assert new.skill_id.startswith("SKILL-")
    assert new.source_candidate_id.startswith("IMPORTED-")
    assert new.name == "Imported skill"
    # No task_id so the event is global.
    types = {ev["event_type"] for ev in context.repo._events}  # type: ignore[attr-defined]
    assert "skill_card_imported" in types


def test_import_rejects_unsupported_schema_version(
    context: CliContext, tmp_path: Path
) -> None:
    yaml_path = tmp_path / "wrong.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"schema_version": "2", "name": "x"}),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "import", str(yaml_path)], obj=context)
    assert result.exit_code == 2
    assert "Unsupported schema_version" in result.output


def test_import_rejects_non_mapping_root(
    context: CliContext, tmp_path: Path
) -> None:
    yaml_path = tmp_path / "list.yaml"
    yaml_path.write_text(yaml.safe_dump([1, 2, 3]), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "import", str(yaml_path)], obj=context)
    assert result.exit_code == 2
    assert "mapping" in result.output


# ---------------------------------------------------------------------------
# skill show
# ---------------------------------------------------------------------------


def test_show_finds_candidate_by_id(context: CliContext) -> None:
    _seed_candidate(context.repo)
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "show", "skill-cand-1"], obj=context)
    assert result.exit_code == 0
    assert "SkillCardCandidate" in result.output
    assert "Test skill" in result.output


def test_show_finds_active_by_skill_id(context: CliContext) -> None:
    repo = context.repo
    repo.create_task("t1", "Goal")
    repo.save_active_skill_card(
        ActiveSkillCard(
            skill_id="SKILL-abc",
            source_candidate_id="cand-x",
            name="Already active",
            activated_at=datetime.now(timezone.utc),
            activated_by="alice",
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "show", "SKILL-abc"], obj=context)
    assert result.exit_code == 0
    assert "ActiveSkillCard" in result.output
    assert "Already active" in result.output


def test_show_unknown_id_usage_error(context: CliContext) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["skill", "show", "nope"], obj=context)
    assert result.exit_code != 0
    assert "no skill" in result.output


# ---------------------------------------------------------------------------
# skill list extensions
# ---------------------------------------------------------------------------


def test_list_state_filter_active_only(context: CliContext) -> None:
    repo = context.repo
    _seed_candidate(repo, skill_candidate_id="cand-1")
    repo.save_active_skill_card(
        ActiveSkillCard(
            skill_id="SKILL-active",
            source_candidate_id="cand-x",
            name="active one",
            activated_at=datetime.now(timezone.utc),
            activated_by="alice",
        )
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["skill", "list", "--state", "active"], obj=context
    )
    assert result.exit_code == 0
    assert "SKILL-active" in result.output
    assert "cand-1" not in result.output
