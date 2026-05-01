"""Planning models for HungerLoop v0.4.1.

:class:`LoopPlan` describes which hunger items to work on in a loop iteration.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from hungerloop.models.enums import LoopPhase


class Assignment(BaseModel):
    """Agent assignment within a loop plan."""

    agent_id: str
    mission: str
    target_hunger_item_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)


class LoopPlan(BaseModel):
    """Plan for one loop iteration."""

    task_id: str
    loop_id: int
    selected_hunger_item_ids: list[str] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    phase: LoopPhase = LoopPhase.EXPLORE


class BudgetAllocation(BaseModel):
    """Budget allocation per phase."""

    phase: LoopPhase
    max_tokens: int = 4000
    max_tool_calls: int = 10
