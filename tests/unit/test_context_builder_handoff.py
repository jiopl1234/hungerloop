from __future__ import annotations

from hungerloop.models.context import ContextPack
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation
from hungerloop.services.context_builder import _clip_recent_summaries


def test_context_pack_prior_handoff_summary_default() -> None:
    pack = ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="mission",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="candidates/loop_001",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert pack.prior_handoff_summary == ""


def test_handoff_takes_precedence_over_self_summary() -> None:
    prior_handoff_summary, last_self_summary = _clip_recent_summaries(
        prior_handoff_summary="H" * 1500,
        last_summary="S" * 1500,
    )

    assert len(prior_handoff_summary) == 1500
    assert last_self_summary is not None
    assert len(last_self_summary) <= 500
