"""Planning models for HungerLoop v0.5a.

:class:`LoopPlan` describes which hunger items to work on in a loop iteration.
:class:`BudgetAllocation` is the per-loop / per-worker resource envelope, with
fields covering wall-clock, retry, and side-effect policy gates (PRD §4.2,
§28.11, §28.18).
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

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
    rationale: str = ""


class BudgetAllocation(BaseModel):
    """Per-phase / per-worker budget envelope.

    The envelope spans three orthogonal dimensions:

    1. **Resources** — ``max_tokens``, ``max_tool_calls``, ``max_wall_clock_seconds``.
       Enforced by :class:`~hungerloop.services.budget_guard.BudgetGuard` (M12 / ADR-002).
    2. **Concurrency** — ``max_workers_per_loop`` (planner-side; v0.5a fixed to 1).
    3. **Retry policy** — ``max_model_retries``, ``retry_base_delay_seconds``,
       ``retry_max_delay_seconds``. Consumed by ``ModelClient.complete_json``
       (M6 / ADR-004); ``retry_max_delay_seconds`` is also the ceiling for
       ``Retry-After``-driven sleeps.
    4. **Side-effect policy** — ``allow_shell``, ``allow_file_write``,
       ``allow_network``. Enforced by :class:`ToolHarness` (PRD §28.11).

    Pydantic ``model_validator`` rejects retry max < base.
    """

    phase: LoopPhase

    # Resources
    max_tokens: int = Field(default=4000, ge=0)
    max_tool_calls: int = Field(default=10, ge=0)
    max_wall_clock_seconds: int = Field(default=300, ge=1)

    # Concurrency
    max_workers_per_loop: int = Field(default=1, ge=1)
    max_new_items_per_loop: int = Field(default=3, ge=0)

    # Retry policy (read by ModelClient.complete_json)
    max_model_retries: int = Field(default=2, ge=0)
    retry_base_delay_seconds: float = Field(default=1.0, ge=0.0)
    retry_max_delay_seconds: float = Field(default=20.0, ge=0.0)

    # Side-effect policy gates (enforced by ToolHarness)
    allow_shell: bool = True
    allow_file_write: bool = True
    allow_network: bool = False

    @model_validator(mode="after")
    def _validate_retry_window(self) -> BudgetAllocation:
        """Reject retry_max_delay_seconds < retry_base_delay_seconds."""
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError(
                f"retry_max_delay_seconds ({self.retry_max_delay_seconds}) "
                f"must be >= retry_base_delay_seconds ({self.retry_base_delay_seconds})"
            )
        return self
