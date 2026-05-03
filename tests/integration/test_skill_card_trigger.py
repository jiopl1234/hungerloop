"""End-to-end SkillCard trigger: DONE + ≥2 accepted checks (PRD §23.2)."""
from __future__ import annotations

from pathlib import Path

from hungerloop.cli.orchestrator_factory import build_orchestrator
from hungerloop.models.enums import StopReason
from hungerloop.repository.in_memory_repo import InMemoryRepository
from hungerloop.services.model_client import DummyModelClient
from hungerloop.services.skill_manager import SkillManager
from tests.integration.conftest import make_two_check_seed, workspace


async def test_skill_card_emitted_on_done_with_two_checks(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_two_check_seed()(repo)

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
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    assert report.stop_reason is StopReason.DONE

    card = SkillManager(repo).maybe_create_skill_card("t1", report)
    assert card is not None
    assert len(card.accepted_check_keys) >= 2

    cards = repo.list_skill_cards("t1")
    assert len(cards) == 1
    assert cards[0].skill_id == card.skill_id


async def test_skill_card_skipped_when_only_one_check_accepted(tmp_path: Path) -> None:
    repo = InMemoryRepository()
    make_two_check_seed()(repo)

    # Only satisfy the first check; second never lands.
    actions = [
        {
            "tool_name": "write_file",
            "args": {"path": "report.md", "content": "# r\n"},
        }
    ]
    orchestrator = build_orchestrator(
        repo=repo,
        workspace_root=workspace(tmp_path),
        model_client=DummyModelClient.with_actions(actions),
    )
    orchestrator.workspace_manager.ensure_task_workspace("t1")

    report = await orchestrator.run("t1")
    # Item never fully satisfied → no DONE; skill manager refuses regardless.
    card = SkillManager(repo).maybe_create_skill_card("t1", report)
    assert card is None
