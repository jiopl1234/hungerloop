from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from hungerloop.models.enums import HungerItemStatus
from hungerloop.models.events import EventType
from hungerloop.models.handoff import DiscoveredFact, HandoffProcessingResult
from hungerloop.models.mission import Mission
from hungerloop.models.planning import BudgetAllocation
from hungerloop.models.synthesis import CheckProposal
from hungerloop.models.worker import HandoffItem, WorkerHandoff
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.check_proposal_gate import CheckProposalGate
from hungerloop.services.refinement_compiler import RefinementCompiler
from hungerloop.services.requirement_compiler import RequirementCompiler

if TYPE_CHECKING:
    from hungerloop.services.refactor_transaction_manager import (
        RefactorTransactionManager,
    )


class HandoffProcessor:
    """Route structured worker handoffs into repository updates.

    v0.7: When a ``CheckProposalGate`` and ``RefinementCompiler`` are
    provided, ``process_handoffs`` becomes async so it can ``await`` the
    gate exactly once for each batch of worker proposals collected from
    discovered test-gap handoff items.  Accepted proposals are injected
    through ``RefinementCompiler.compile_spec_coverage`` with
    ``generated_by=f"worker:{agent_id}"``, keeping ledger writes
    compiler-owned.
    """

    def __init__(
        self,
        repo: RepositoryProtocol,
        *,
        requirement_compiler: RequirementCompiler | None = None,
        check_proposal_gate: CheckProposalGate | None = None,
        refinement_compiler: RefinementCompiler | None = None,
        refactor_transaction_manager: RefactorTransactionManager | None = None,
    ) -> None:
        self.repo = repo
        self.requirement_compiler = requirement_compiler or RequirementCompiler(repo)
        self.check_proposal_gate = check_proposal_gate
        self.refinement_compiler = refinement_compiler
        self.refactor_transaction_manager = refactor_transaction_manager

    async def process_handoffs(
        self,
        task_id: str,
        loop_id: int,
        handoffs: list[WorkerHandoff],
        *,
        mission: Mission | None,
        budget: BudgetAllocation,
    ) -> HandoffProcessingResult:
        blocked_item_ids: list[str] = []
        discovered_issues: list[DiscoveredFact] = []
        injected_hunger_item_ids: list[str] = []
        summary_lines: list[str] = []
        critical_lines: list[str] = []
        discovered_issue_count = 0
        cap = budget.max_new_items_per_loop

        # Collected proposals from test-gap discovered_issue items.
        # Each entry is (proposal, agent_id, source_handoff_id).
        collected_proposals: list[tuple[CheckProposal, str, str]] = []

        for handoff in handoffs:
            for item_index, item in enumerate(handoff.handoff_items):
                if item.item_type == "blocker":
                    for related_item_id in item.related_item_ids:
                        scope = self._scope_payload(mission, item)
                        current = self.repo.get_hunger_item(related_item_id)
                        if current is not None and current.status is HungerItemStatus.CLOSED:
                            self.repo.append_event(
                                EventType.HANDOFF_BLOCKER_ON_CLOSED_ITEM,
                                {
                                    **scope,
                                    "agent_id": handoff.agent_id,
                                    "item_id": related_item_id,
                                    "source_summary": self._handoff_text(item),
                                },
                                task_id=task_id,
                                loop_id=loop_id,
                            )
                            continue
                        self.repo.update_hunger_item_status(
                            task_id,
                            related_item_id,
                            HungerItemStatus.BLOCKED,
                        )
                        if related_item_id not in blocked_item_ids:
                            blocked_item_ids.append(related_item_id)
                        self.repo.append_event(
                            EventType.WORKER_HANDOFF_BLOCKER_RECORDED,
                            {
                                **scope,
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
                        # Collect proposals only after fact succeeds,
                        # avoiding unbound or stale fact references.
                        if (
                            fact.kind == "test_gap"
                            and item.proposed_checks
                        ):
                            for proposal in item.proposed_checks:
                                collected_proposals.append(
                                    (proposal, handoff.agent_id, source_handoff_id)
                                )
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

                if item.item_type == "refactor_proposal":
                    self._process_refactor_proposal(
                        task_id=task_id,
                        loop_id=loop_id,
                        item=item,
                        agent_id=handoff.agent_id,
                    )
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

        # Process collected worker proposals through the gate and compiler.
        accepted_proposal_count = 0
        if collected_proposals and self.check_proposal_gate is not None:
            remaining_cap = max(0, cap - discovered_issue_count)
            injected_proposal_ids, accepted_proposal_count = (
                await self._process_worker_proposals(
                    task_id=task_id,
                    loop_id=loop_id,
                    collected_proposals=collected_proposals,
                    remaining_cap=remaining_cap,
                )
            )
            # Use the exact ids returned by the compiler, not a ledger scan.
            injected_hunger_item_ids.extend(injected_proposal_ids)

        result = HandoffProcessingResult(
            prior_handoff_summary="\n".join([*critical_lines, *summary_lines]).strip(),
            discovered_issues=discovered_issues,
            blocked_item_ids=blocked_item_ids,
            injected_hunger_item_ids=injected_hunger_item_ids,
            accepted_proposal_count=accepted_proposal_count,
        )
        self.repo.save_handoff_processing_result(task_id, result)
        return result

    async def _process_worker_proposals(
        self,
        *,
        task_id: str,
        loop_id: int,
        collected_proposals: list[tuple[CheckProposal, str, str]],
        remaining_cap: int,
    ) -> tuple[list[str], int]:
        """Gate and inject worker proposals through the compiler.

        Returns a tuple of (injected_ids, accepted_count) where
        ``injected_ids`` are the exact ids returned by the compiler
        during this processing pass and ``accepted_count`` is the
        number of proposals actually injected.

        Cap exhaustion is deterministic: accepted proposals are
        processed in the order they were collected (preserving handoff
        and item order).  Proposals that cannot be injected due to
        cap exhaustion emit ``WORKER_PROPOSAL_CAP_EXHAUSTED`` events
        with stable, non-secret payloads.
        """
        if self.check_proposal_gate is None:
            return [], 0

        # Build existing dedup keys from the ledger and rejected proposal history.
        existing_keys = self._collect_existing_keys(task_id)

        # Extract just the proposals for the gate.
        proposals = [p for p, _, _ in collected_proposals]

        # Await the gate exactly once for the batch.
        gate_result = await self.check_proposal_gate.filter(
            proposals,
            existing_keys=existing_keys,
        )

        # Emit rejection events for rejected proposals.
        for rejected in gate_result.rejected:
            # Find the source agent_id and handoff_id for this proposal.
            source_agent = "unknown"
            source_handoff_id = "unknown"
            for prop, agent_id, handoff_id in collected_proposals:
                if prop is rejected.proposal:
                    source_agent = agent_id
                    source_handoff_id = handoff_id
                    break
            self.repo.append_event(
                EventType.SYNTH_CHECK_REJECTED,
                {
                    "source": "worker_proposal",
                    "agent_id": source_agent,
                    "source_handoff_id": source_handoff_id,
                    "dedup_key": rejected.dedup_key,
                    "reason": rejected.reason,
                    "check_type": rejected.proposal.check_type.value,
                },
                task_id=task_id,
                loop_id=loop_id,
            )

        if not gate_result.accepted or self.refinement_compiler is None:
            return [], 0

        # Build a lookup from proposal identity to (agent_id, source_handoff_id)
        # so we can group accepted proposals by agent while preserving
        # collection order for deterministic cap consumption.
        all_injected_ids: list[str] = []
        total_accepted = 0
        cap_remaining = remaining_cap

        # Group accepted proposals by agent_id, preserving collection order.
        # This ensures deterministic cap consumption: earlier-collected
        # proposals are injected first.
        accepted_by_agent: dict[str, list[CheckProposal]] = {}
        agent_order: list[str] = []
        for accepted_proposal in gate_result.accepted:
            source_agent = "unknown"
            for prop, agent_id, _ in collected_proposals:
                if prop is accepted_proposal:
                    source_agent = agent_id
                    break
            if source_agent not in accepted_by_agent:
                accepted_by_agent[source_agent] = []
                agent_order.append(source_agent)
            accepted_by_agent[source_agent].append(accepted_proposal)

        for agent_id in agent_order:
            agent_proposals = accepted_by_agent[agent_id]
            if cap_remaining <= 0:
                # Cap exhausted: emit stable cap-rejection events for
                # the remaining accepted proposals from this agent.
                for proposal in agent_proposals:
                    source_handoff_id = "unknown"
                    for prop, a_id, h_id in collected_proposals:
                        if prop is proposal:
                            source_handoff_id = h_id
                            break
                    self.repo.append_event(
                        "WORKER_PROPOSAL_CAP_EXHAUSTED",
                        {
                            "source": "worker_proposal",
                            "agent_id": agent_id,
                            "source_handoff_id": source_handoff_id,
                            "dedup_key": proposal.dedup_key(),
                            "check_type": proposal.check_type.value,
                        },
                        task_id=task_id,
                        loop_id=loop_id,
                    )
                continue

            generated_by = f"worker:{agent_id}"
            injected_ids = self.refinement_compiler.compile_spec_coverage(
                task_id=task_id,
                proposals=agent_proposals,
                generated_by=generated_by,
                tier=1,
                max_new_items=cap_remaining,
            )
            all_injected_ids.extend(injected_ids)
            total_accepted += len(injected_ids)
            cap_remaining -= len(injected_ids)

            # If the compiler injected fewer items than submitted
            # (e.g. some were duplicates at the compiler level or cap
            # was hit), emit a stable cap-exhaustion event noting the
            # count of uninjected proposals.
            uninjected_count = len(agent_proposals) - len(injected_ids)
            if uninjected_count > 0:
                self.repo.append_event(
                    "WORKER_PROPOSAL_CAP_EXHAUSTED",
                    {
                        "source": "worker_proposal",
                        "agent_id": agent_id,
                        "uninjected_count": uninjected_count,
                    },
                    task_id=task_id,
                    loop_id=loop_id,
                )

        return all_injected_ids, total_accepted

    def _collect_existing_keys(self, task_id: str) -> set[str]:
        """Collect dedup keys from existing ledger checks and rejected proposal history."""
        from hungerloop.services.refinement_compiler import _collect_existing_dedup_keys

        ledger = self.repo.get_hunger_ledger(task_id)
        keys = _collect_existing_dedup_keys(ledger)

        # Also include rejected proposal dedup keys from event history.
        rejected_events = self.repo.list_events(
            task_id,
            event_types=[EventType.SYNTH_CHECK_REJECTED.value],
        )
        for event in rejected_events:
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                dedup_key = payload.get("dedup_key")
                if isinstance(dedup_key, str):
                    keys.add(dedup_key)

        return keys

    def _process_refactor_proposal(
        self,
        *,
        task_id: str,
        loop_id: int,
        item: HandoffItem,
        agent_id: str,
    ) -> None:
        """Route a ``refactor_proposal`` handoff item to the transaction manager.

        This method affects only transaction state. It never writes hunger
        ledger items or mission artifacts (VAL-REF-015). When the transaction
        manager is not configured, the proposal is silently ignored.
        """
        if self.refactor_transaction_manager is None:
            return

        payload = item.refactor_proposal_payload
        if payload is None:
            self.repo.append_event(
                EventType.REFACTOR_TXN_OPEN_REJECTED,
                {
                    "task_id": task_id,
                    "loop_id": loop_id,
                    "agent_id": agent_id,
                    "reason": "missing_refactor_payload",
                },
                task_id=task_id,
                loop_id=loop_id,
            )
            return

        if payload.action == "open":
            self.refactor_transaction_manager.open(
                task_id=task_id,
                loop_id=loop_id,
                declared_regression_keys=payload.declared_regression_keys,
                rationale=payload.rationale,
            )
        elif payload.action == "close":
            self.refactor_transaction_manager.close(
                task_id=task_id,
                loop_id=loop_id,
                force=True,
            )

    @staticmethod
    def _scope_payload(
        mission: Mission | None,
        item: HandoffItem,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "mission_id": mission.mission_id if mission is not None else None,
            "phase_id": None,
            "feature_id": None,
        }
        if mission is None:
            return payload

        related_features = set(item.related_feature_ids)
        related_items = set(item.related_item_ids)
        for feature in mission.features:
            if (
                feature.feature_id in related_features
                or feature.hunger_item_id in related_items
            ):
                payload["phase_id"] = feature.phase_id
                payload["feature_id"] = feature.feature_id
                return payload
        return payload

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
