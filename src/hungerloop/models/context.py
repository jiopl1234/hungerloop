"""Context models for HungerLoop v0.5a.

:class:`ContextPack` is the agent's execution context for a single loop
iteration. The ``budget`` field is now typed as :class:`BudgetAllocation`
(PRD §4.3 / §28.7); the v0.4.1 ``dict[str, object]`` shape is gone so callers
can use attribute access (``context.budget.max_wall_clock_seconds``).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from hungerloop.models.planning import BudgetAllocation


class TruncationInfo(BaseModel):
    """Audit metadata for total prior-loop block truncation.

    ``ContextBuilder`` may clip individual sections before assembling the
    block. This model is emitted only when the final rendered prior-loop block
    itself overflowed and entries had to be dropped.
    """

    chars_before: int = Field(..., ge=0)
    chars_after: int = Field(..., ge=0)
    dropped_failures: int = Field(0, ge=0)
    dropped_evidence: int = Field(0, ge=0)
    degraded_failures: int = Field(0, ge=0)
    truncated_best_summary: bool = False


class ContextPack(BaseModel):
    """Agent execution context for one loop iteration."""

    task_id: str
    loop_id: int
    agent_id: str
    mission: str
    phase: str

    target_hunger_item_ids: list[str]
    target_feature_ids: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    # Per-criterion check_key, parallel to acceptance_criteria: i-th
    # entry is the canonical key for the i-th rendered criterion. Lets
    # ExecutionWorker._messages tag each criterion with [<key>] so the
    # model can cross-reference the pass/fail status block by key.
    # Empty when callers (legacy tests) construct ContextPack directly
    # without going through ContextBuilder; in that case _messages falls
    # back to the keyless format.
    acceptance_check_keys: list[str] = Field(default_factory=list)
    # Per-assignment check-key splits derived from BestState before this
    # loop runs. `passed_check_keys` are check_keys for THIS assignment's
    # target items that are already in `best.accepted_check_keys`;
    # `failing_check_keys` are the remaining check_keys. Both are
    # generic harness data — just identifier strings the operator already
    # put in the mission ledger. Used by ExecutionWorker._messages to
    # tell the model "these N already pass, focus on these M" so the
    # worker does not waste inner-iters re-discovering state.
    passed_check_keys: list[str] = Field(default_factory=list)
    failing_check_keys: list[str] = Field(default_factory=list)

    best_state_summary: str | None = None
    best_workspace_ref: str = "best"
    candidate_workspace_ref: str

    relevant_claim_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    failure_patterns_to_avoid: list[str] = Field(default_factory=list)
    last_self_summary: str | None = None
    prior_handoff_summary: str = ""
    relevant_evidence_summaries: list[str] = Field(default_factory=list)
    best_workspace_files: list[str] = Field(default_factory=list)
    truncation_info: TruncationInfo | None = None

    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)

    # v0.7: cross-task promoted memory recall. Populated by ContextBuilder
    # when ``memory_recall_enabled`` is True. Each entry is a prompt-safe
    # reusable insight string from a promoted memory originating in a
    # different task. Default empty so direct construction without recall
    # remains valid (VAL-MEM-009).
    recalled_memories: list[str] = Field(default_factory=list)

    budget: BudgetAllocation
    required_output_schema: str = ""
