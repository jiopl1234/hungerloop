from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.handoff import DiscoveredFact, HandoffProcessingResult
from hungerloop.models.mission import Mission
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.requirement_compiler import RequirementCompiler


class HandoffProcessor:
    """Route structured worker handoffs into repository updates."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        *,
        requirement_compiler: RequirementCompiler | None = None,
    ) -> None:
        self.repo = repo
        self.requirement_compiler = requirement_compiler or RequirementCompiler(repo)

    def process_handoffs(
        self,
        task_id: str,
        loop_id: int,
        handoffs: list[WorkerHandoff],
        *,
        mission: Mission | None,
        budget: BudgetAllocation,
    ) -> HandoffProcessingResult:
        del mission
        blocked_item_ids: list[str] = []
        discovered_issues: list[DiscoveredFact] = []
        injected_hunger_item_ids: list[str] = []
        summary_lines: list[str] = []
        critical_lines: list[str] = []
        discovered_issue_count = 0
        cap = budget.max_new_items_per_loop

        for handoff in handoffs:
            for item_index, item in enumerate(handoff.handoff_items):
                if item.item_type == "blocker":
                    for related_item_id in item.related_item_ids:
                        self.repo.update_hunger_item_status(
                            task_id,
                            related_item_id,
                            HungerItemStatus.BLOCKED,
                        )
                        if related_item_id not in blocked_item_ids:
                            blocked_item_ids.append(related_item_id)
                        self.repo.append_event(
                            "worker.handoff_blocker_recorded",
                            {
                                "agent_id": handoff.agent_id,
                                "item_id": related_item_id,
                                "source_summary": self._handoff_text(item),
                            },
                            task_id=task_id,
                            loop_id=loop_id,
                        )
                    continue

                if item.item_type == "discovered_issue":
                    if discovered_issue_count >= cap:
                        summary_lines.append(f"Follow-up: {self._handoff_text(item)}")
                        continue
                    source_handoff_id = self._source_handoff_id(handoff, item_index)
                    try:
                        fact = self._fact_from_handoff_item(item, source_handoff_id)
                        injected_ids = self.requirement_compiler.compile_discovered_facts(
                            task_id,
                            [fact],
                            budget=budget,
                        )
                        discovered_issues.append(fact)
                        injected_hunger_item_ids.extend(injected_ids)
                        discovered_issue_count += 1
                    except ValidationError as exc:
                        self.repo.append_event(
                            "DISCOVERED_FACT_REJECTED",
                            {
                                "agent_id": handoff.agent_id,
                                "source_handoff_id": source_handoff_id,
                                "summary": self._handoff_text(item),
                                "error": str(exc),
                            },
                            task_id=task_id,
                            loop_id=loop_id,
                        )
                        summary_lines.append(f"Follow-up: {self._handoff_text(item)}")
                    continue

                if item.item_type == "follow_up":
                    summary_lines.append(f"Follow-up: {self._handoff_text(item)}")
                    continue

                if item.item_type == "incomplete_work":
                    for feature_id in item.related_feature_ids:
                        self.repo.update_feature_status(feature_id, "in_progress")
                    continue

                if item.item_type == "critical_context":
                    critical_lines.insert(0, f"[CRITICAL] {self._handoff_text(item)}")

        return HandoffProcessingResult(
            prior_handoff_summary="\n".join([*critical_lines, *summary_lines]).strip(),
            discovered_issues=discovered_issues,
            blocked_item_ids=blocked_item_ids,
            injected_hunger_item_ids=injected_hunger_item_ids,
        )

    @staticmethod
    def _source_handoff_id(handoff: WorkerHandoff, item_index: int) -> str:
        return (
            f"WH-{handoff.task_id}-{handoff.loop_id}-"
            f"{handoff.agent_id}-{item_index}"
        )

    @staticmethod
    def _handoff_text(item: HandoffItem) -> str:
        summary = item.summary.strip()
        if summary:
            return summary
        detail = item.detail.strip()
        if detail:
            return detail
        return "unspecified handoff item"

    def _fact_from_handoff_item(
        self,
        item: HandoffItem,
        source_handoff_id: str,
    ) -> DiscoveredFact:
        description = item.detail.strip() or item.summary.strip()
        text = f"{item.summary} {item.detail}".lower()
        kind: Literal["mission_feature", "blocker_note", "test_gap"]
        if item.related_feature_ids:
            kind = "mission_feature"
        elif any(
            marker in text
            for marker in ("test", "pytest", "ruff", "mypy", "assert", "check")
        ):
            kind = "test_gap"
        else:
            kind = "blocker_note"
        return DiscoveredFact(
            kind=kind,
            title=self._handoff_text(item),
            description=description or "No additional detail provided.",
            source_handoff_id=source_handoff_id,
            related_feature_ids=list(item.related_feature_ids),
        )
