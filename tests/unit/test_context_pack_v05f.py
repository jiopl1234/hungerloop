"""v0.5f ContextPack additive field tests."""
from __future__ import annotations

from hungerloop.models.context import ContextPack, TruncationInfo
from hungerloop.models.enums import LoopPhase
from hungerloop.models.planning import BudgetAllocation


def test_context_pack_v05f_defaults() -> None:
    pack = ContextPack(
        task_id="t1",
        loop_id=1,
        agent_id="execution_worker_v1",
        mission="m",
        phase=LoopPhase.EXPLORE.value,
        target_hunger_item_ids=["H-001"],
        candidate_workspace_ref="candidates/loop_001",
        budget=BudgetAllocation(phase=LoopPhase.EXPLORE),
    )

    assert pack.last_self_summary is None
    assert pack.relevant_evidence_summaries == []
    assert pack.best_workspace_files == []
    assert pack.truncation_info is None


def test_truncation_info_round_trips() -> None:
    info = TruncationInfo(
        chars_before=2400,
        chars_after=1990,
        dropped_failures=2,
        dropped_evidence=3,
        truncated_best_summary=False,
    )

    assert TruncationInfo.model_validate(info.model_dump()) == info
