"""End-to-end skill lifecycle (PRD §18 / E1-13).

Run a demo task → assert SkillCardCandidate created → approve →
ActiveSkillCard exists → export to YAML → import the YAML on a
fresh database → assert the import landed with a fresh skill_id
and IMPORTED- source_candidate_id.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from hungerloop.cli.context import CliContext
from hungerloop.cli.main import cli
from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.repository.sqlite_repo import SQLiteRepository
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.skill_manager import SkillManager
from tests.integration.conftest import make_two_check_seed, workspace


async def test_skill_full_lifecycle_run_approve_export_import(
    tmp_path: Path,
) -> None:
    repo = InMemoryRepository()
    make_two_check_seed()(repo)
    workspace_root = workspace(tmp_path)

    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# r\n"},
        },
        {
            "tool_name": "write_file",
            "args": {"path": "summary.md", "content": "# s\n"},
        },
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace_root,
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.DONE

    # ---- Step 1: candidate created --------------------------------------
    candidate = SkillManager(repo).maybe_create_skill_card("t1", report)
    assert candidate is not None
    candidate_id = candidate.skill_candidate_id

    # ---- Step 2: approve via CLI ---------------------------------------
    ctx = CliContext(repo=repo, workspace_root=workspace_root)
    runner = CliRunner()
    approve = runner.invoke(
        cli,
        ["skill", "approve", candidate_id, "--activated-by", "alice"],
        obj=ctx,
    )
    assert approve.exit_code == 0, approve.output
    actives = repo.list_active_skill_cards()
    assert len(actives) == 1
    skill_id = actives[0].skill_id
    assert actives[0].activated_by == "alice"

    # ---- Step 3: export ------------------------------------------------
    yaml_path = tmp_path / "skill.yaml"
    export = runner.invoke(
        cli,
        ["skill", "export", skill_id, "--output", str(yaml_path)],
        obj=ctx,
    )
    assert export.exit_code == 0, export.output
    assert yaml_path.exists()

    # ---- Step 4: import the YAML into a *fresh* SQLite DB --------------
    fresh_db = tmp_path / "fresh.sqlite"
    fresh_repo = SQLiteRepository.open(fresh_db)
    fresh_ctx = CliContext(repo=fresh_repo, workspace_root=workspace_root)

    import_ = runner.invoke(
        cli, ["skill", "import", str(yaml_path)], obj=fresh_ctx
    )
    assert import_.exit_code == 0, import_.output

    imported = fresh_repo.list_active_skill_cards()
    assert len(imported) == 1
    new = imported[0]
    # Fresh skill_id (must NOT collide with the original).
    assert new.skill_id != skill_id
    assert new.skill_id.startswith("SKILL-")
    # Synthetic source_candidate_id marks the import provenance.
    assert new.source_candidate_id.startswith("IMPORTED-")
    # Field round-trip.
    assert new.name == actives[0].name
    assert new.accepted_check_keys == actives[0].accepted_check_keys
    assert new.steps == actives[0].steps
    # Audit event present (global — task_id IS NULL).
    types = {ev["event_type"] for ev in fresh_repo.list_events("t1", include_global=True)}
    assert "skill_card_imported" in types
