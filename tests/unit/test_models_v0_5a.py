"""Unit tests for new v0.5a models.

Covers UsageSnapshot, MemoryCandidate, SkillCard, Artifact, AgentSpec.kind.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hungerloop.models.blackboard import Artifact
from hungerloop.models.memory import MemoryCandidate
from hungerloop.models.skill import SkillCard
from hungerloop.models.usage import ModelUsage, UsageSnapshot
from hungerloop.models.worker import AgentSpec


def test_usage_snapshot_defaults() -> None:
    snap = UsageSnapshot(task_id="t1")
    assert snap.tokens == 0
    assert snap.cost_usd == 0.0
    assert snap.llm_calls == 0
    assert snap.tool_calls == 0


def test_model_usage_defaults() -> None:
    usage = ModelUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


def test_memory_candidate_defaults() -> None:
    mc = MemoryCandidate(candidate_id="mc-1", task_id="t1", content="fact")
    assert mc.memory_type == "fact"
    assert mc.action_verified is False
    assert mc.reusable is False
    assert mc.non_volatile is False
    assert mc.traceable is False
    assert mc.confidence == 0.0
    assert mc.status == "candidate"


def test_memory_candidate_type_literal() -> None:
    """memory_type is a Literal; invalid values rejected."""
    with pytest.raises(ValidationError):
        MemoryCandidate(
            candidate_id="mc-1",
            task_id="t1",
            content="x",
            memory_type="unknown",  # type: ignore[arg-type]
        )


def test_skill_card_minimal() -> None:
    card = SkillCard(skill_id="sk-1", task_id="t1", name="Fix test")
    assert card.task_description == ""
    assert card.trigger_signals == []
    assert card.created_at == ""


def test_artifact_minimal() -> None:
    art = Artifact(
        artifact_id="art-1",
        task_id="t1",
        loop_id=1,
        artifact_type="patch",
    )
    assert art.path is None
    assert art.summary == ""


def test_agent_spec_kind_default() -> None:
    spec = AgentSpec(agent_id="a1", name="Worker")
    assert spec.kind == "execution"


def test_agent_spec_kind_literal() -> None:
    """kind is AgentKind Literal; invalid values rejected."""
    with pytest.raises(ValidationError):
        AgentSpec(agent_id="a1", name="W", kind="unknown")  # type: ignore[arg-type]
