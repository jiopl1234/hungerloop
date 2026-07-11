"""Deterministic refinement-tier hunger item generation.

v0.5f.4 keeps requirement generation rule-based: once tier-0 correctness
is done, this compiler can add concrete, check-backed refinement items
while the user-provided loop budget remains.

v0.7 adds ``compile_spec_coverage`` as the sole proposal ledger-write
path for accepted check proposals (spec synthesis and worker discovery).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import (
    AcceptanceCheckType,
    CompletionMode,
    HungerItemStatus,
    HungerItemType,
)
from hungerloop.models.events import EventType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.models.synthesis import (
    CheckProposal,
    compute_dedup_key,
)
from hungerloop.repository.protocol import RepositoryProtocol

_PROFILE_PYTHON_MEDIUM = "python_medium"

# Default per-call cap for compile_spec_coverage.
_DEFAULT_MAX_NEW_ITEMS = 20


class RefinementCompileResult(BaseModel):
    """Result of one refinement compiler pass."""

    added_item_ids: list[str] = Field(default_factory=list)
    active_tier: int | None = None
    exhausted: bool = False
    rationale: str = ""


@dataclass(frozen=True)
class _ProfileItem:
    tier: int
    kind: str
    suffix: str
    title: str
    argv: tuple[str, ...]
    description: str


_PYTHON_MEDIUM_ITEMS: tuple[_ProfileItem, ...] = (
    _ProfileItem(
        tier=1,
        kind="tests",
        suffix="tests",
        title="Run the Python test suite",
        argv=("python", "-m", "pytest", "-q"),
        description="python pytest suite passes",
    ),
    _ProfileItem(
        tier=2,
        kind="quality",
        suffix="ruff",
        title="Run ruff static checks",
        argv=("ruff", "check", "src", "tests"),
        description="ruff static checks pass",
    ),
    _ProfileItem(
        tier=2,
        kind="typecheck",
        suffix="mypy",
        title="Run mypy strict type checks",
        argv=("mypy", "--strict", "src/"),
        description="mypy strict type checks pass",
    ),
)


class RefinementCompiler:
    """Generate deterministic refinement hunger items for enabled profiles."""

    def __init__(self, repo: RepositoryProtocol) -> None:
        self.repo = repo

    def compile_spec_coverage(
        self,
        *,
        task_id: str,
        proposals: list[CheckProposal],
        generated_by: str,
        tier: int = 1,
        max_new_items: int = _DEFAULT_MAX_NEW_ITEMS,
        baseline_pending: bool = False,
    ) -> list[str]:
        """Compile accepted proposals into ``H-SYN-NNN`` hunger items.

        This is the sole ledger-write path for accepted proposal checks.
        It creates ``H-SYN-*`` hunger items with:

        - ``refinement_kind="spec_coverage"``
        - ``refinement_tier`` set to the supplied ``tier``
        - ``generated_by`` set to the supplied value (not the nested
          ``proposed_by`` on the proposal)
        - an acceptance check whose type, params, and description match
          the proposal

        Deduplication is performed against existing ledger acceptance
        checks and existing ``H-SYN`` items, so repeated compilation of
        proposals already present in the ledger does not add duplicates.

        ``max_new_items=0`` is a valid no-op: no items are injected and
        no repository save occurs.  When no item is new, the ledger is
        not saved.
        """
        if max_new_items <= 0 or not proposals:
            return []

        ledger = self.repo.get_hunger_ledger(task_id)

        # Compute dedup keys from existing ledger acceptance checks.
        existing_keys = _collect_existing_dedup_keys(ledger)

        # Determine the next H-SYN sequence number.
        next_seq = _next_h_syn_sequence(ledger)

        new_items: list[HungerItem] = []
        matched_proposals: list[CheckProposal] = []
        for proposal in proposals:
            if len(new_items) >= max_new_items:
                break

            key = proposal.dedup_key()
            if key in existing_keys:
                continue

            item_id = f"H-SYN-{next_seq:03d}"
            next_seq += 1

            new_items.append(
                _build_spec_coverage_item(
                    item_id=item_id,
                    proposal=proposal,
                    generated_by=generated_by,
                    tier=tier,
                    baseline_pending=baseline_pending,
                )
            )
            matched_proposals.append(proposal)
            existing_keys.add(key)

        if not new_items:
            return []

        updated_ledger = HungerLedger(
            task_id=task_id,
            items=[*ledger.items, *new_items],
        )
        self.repo.save_hunger_ledger(task_id, updated_ledger)
        for item, proposal in zip(new_items, matched_proposals, strict=False):
            self.repo.save_hunger_item(item)
            self.repo.append_event(
                EventType.SYNTHESIS_ITEM_INJECTED,
                {
                    "item_id": item.id,
                    "task_id": task_id,
                    "generated_by": generated_by,
                    "refinement_tier": tier,
                    "refinement_kind": "spec_coverage",
                    "dedup_key": proposal.dedup_key(),
                    "source_quote": proposal.source_quote,
                    "proposed_by": proposal.proposed_by,
                    "check_type": proposal.check_type.value,
                    "description": proposal.description,
                    "baseline_pending": baseline_pending,
                    "fixture_argv": proposal.fixture_argv,
                    "prerequisite_check_keys": proposal.prerequisite_check_keys,
                },
                task_id=task_id,
            )

        return [item.id for item in new_items]


    def complete_synthesis_baseline(
        self,
        *,
        task_id: str,
        item_id: str,
        loop_id: int,
    ) -> None:
        """Mark one synthesized item as baseline-checked."""
        ledger = self.repo.get_hunger_ledger(task_id)
        item = next((entry for entry in ledger.items if entry.id == item_id), None)
        if item is None or not item.synthesis_baseline_pending:
            return
        item.synthesis_baseline_pending = False
        item.updated_at_loop = loop_id
        self.repo.save_hunger_ledger(task_id, ledger)

    def resolve_synthesis_item(
        self,
        *,
        task_id: str,
        item_id: str,
        resolution_kind: str,
        resolution_reason: str,
        loop_id: int,
    ) -> None:
        """Close a synthesized item with an auditable terminal resolution."""
        ledger = self.repo.get_hunger_ledger(task_id)
        item = next((entry for entry in ledger.items if entry.id == item_id), None)
        if item is None:
            return
        item.status = HungerItemStatus.CLOSED
        item.gap_score = 0.0
        item.synthesis_baseline_pending = False
        item.synthesis_resolution_kind = resolution_kind
        item.synthesis_resolution_reason = resolution_reason
        item.updated_at_loop = loop_id
        self.repo.save_hunger_ledger(task_id, ledger)

    def record_synthesis_conflict(
        self,
        *,
        task_id: str,
        item_id: str,
        signature: str,
        conflicting_check_keys: list[str],
        threshold: int,
        loop_id: int,
    ) -> tuple[int, bool]:
        """Persist a conflict occurrence and quarantine at ``threshold``."""
        ledger = self.repo.get_hunger_ledger(task_id)
        item = next((entry for entry in ledger.items if entry.id == item_id), None)
        if item is None:
            return 0, False
        count = item.synthesis_conflict_signatures.get(signature, 0) + 1
        item.synthesis_conflict_signatures[signature] = count
        item.updated_at_loop = loop_id
        quarantined = count >= threshold
        if quarantined:
            item.status = HungerItemStatus.CLOSED
            item.gap_score = 0.0
            item.synthesis_baseline_pending = False
            item.synthesis_resolution_kind = "invalid_synthesis"
            item.synthesis_resolution_reason = (
                "repeated conflict with accepted checks: "
                + ", ".join(conflicting_check_keys)
            )
        self.repo.save_hunger_ledger(task_id, ledger)
        return count, quarantined

    def ensure_next_tier(
        self,
        *,
        task_id: str,
        policy: HungerPolicy,
        ledger: HungerLedger,
        best_state: BestState | None,
    ) -> RefinementCompileResult:
        """Add the next eligible refinement tier, if policy permits it."""
        if policy.completion_mode != CompletionMode.SPEND_BUDGET:
            return RefinementCompileResult(rationale="completion_mode is stop_on_done")
        if policy.refinement_profile != _PROFILE_PYTHON_MEDIUM:
            return RefinementCompileResult(
                exhausted=True,
                rationale=f"unsupported refinement profile: {policy.refinement_profile}",
            )
        if policy.max_refinement_tier <= 0:
            return RefinementCompileResult(
                exhausted=True,
                rationale="max_refinement_tier <= 0",
            )
        if not ledger.tier_is_done(0):
            return RefinementCompileResult(
                active_tier=0,
                rationale="tier 0 is not done",
            )

        for tier in range(1, policy.max_refinement_tier + 1):
            if not ledger.all_tiers_done(tier - 1):
                return RefinementCompileResult(
                    active_tier=tier - 1,
                    rationale=f"lower tier {tier - 1} is not done",
                )
            existing = [item for item in ledger.items if item.refinement_tier == tier]
            if existing:
                if not ledger.tier_is_done(tier):
                    return RefinementCompileResult(
                        active_tier=tier,
                        exhausted=False,
                        rationale=f"tier {tier} already exists",
                    )
                continue

            added = self._add_python_medium_tier(
                task_id=task_id,
                tier=tier,
                ledger=ledger,
                best_state=best_state,
            )
            if added:
                return RefinementCompileResult(
                    added_item_ids=added,
                    active_tier=tier,
                    rationale=f"added tier {tier} refinement items",
                )

        return RefinementCompileResult(
            exhausted=True,
            rationale="all configured refinement tiers are complete or unavailable",
        )

    def _add_python_medium_tier(
        self,
        *,
        task_id: str,
        tier: int,
        ledger: HungerLedger,
        best_state: BestState | None,
    ) -> list[str]:
        source_check_keys = list(best_state.accepted_check_keys) if best_state else []
        new_items: list[HungerItem] = []
        existing_ids = {item.id for item in ledger.items}
        for profile_item in _PYTHON_MEDIUM_ITEMS:
            if profile_item.tier != tier:
                continue
            item_id = f"H-REF-{tier:02d}-{profile_item.suffix}"
            if item_id in existing_ids:
                continue
            new_items.append(
                HungerItem(
                    id=item_id,
                    title=profile_item.title,
                    item_type=HungerItemType.GOAL_GAP,
                    priority=max(0.1, 1.0 - (tier * 0.1)),
                    gap_score=1.0,
                    acceptance_checks=[
                        AcceptanceCheck(
                            check_type=AcceptanceCheckType.SHELL_EXIT_ZERO,
                            params={
                                "argv": list(profile_item.argv),
                                "timeout": 60,
                            },
                            description=profile_item.description,
                        )
                    ],
                    acceptance_mode="all",
                    refinement_tier=tier,
                    refinement_kind=profile_item.kind,
                    generated_by=_PROFILE_PYTHON_MEDIUM,
                    source_check_keys=source_check_keys,
                )
            )

        if not new_items:
            return []

        updated = HungerLedger(task_id=task_id, items=[*ledger.items, *new_items])
        self.repo.save_hunger_ledger(task_id, updated)
        for item in new_items:
            self.repo.save_hunger_item(item)
        return [item.id for item in new_items]


# ---------------------------------------------------------------------------
# Spec-coverage compilation helpers
# ---------------------------------------------------------------------------

_H_SYN_ID_RE = re.compile(r"^H-SYN-(\d+)$")


def _check_dedup_key(check: AcceptanceCheck) -> str | None:
    """Compute a dedup key for an existing acceptance check.

    Returns ``None`` for check types that are not eligible for proposal
    deduplication (i.e. types that ``CheckProposal`` would never produce).
    The key format matches :meth:`CheckProposal.dedup_key` via the shared
    :func:`hungerloop.models.synthesis.compute_dedup_key` function.
    """
    if check.check_type not in (
        AcceptanceCheckType.SHELL_EXIT_ZERO,
        AcceptanceCheckType.FILE_EXISTS,
    ):
        return None
    return compute_dedup_key(
        check_type=check.check_type,
        params=check.params,
    )


def _collect_existing_dedup_keys(ledger: HungerLedger) -> set[str]:
    """Collect dedup keys from all acceptance checks in the ledger.

    This provides idempotent deduplication against existing operator-
    authored checks and previously injected ``H-SYN`` items.
    """
    keys: set[str] = set()
    for item in ledger.items:
        for check in item.acceptance_checks:
            key = _check_dedup_key(check)
            if key is not None:
                keys.add(key)
    return keys


def _next_h_syn_sequence(ledger: HungerLedger) -> int:
    """Return the next sequence number for ``H-SYN-NNN`` ids.

    Scans existing item ids matching ``H-SYN-NNN`` and returns
    ``max + 1`` (or ``1`` if none exist).
    """
    max_seq = 0
    for item in ledger.items:
        match = _H_SYN_ID_RE.match(item.id)
        if match is not None:
            seq = int(match.group(1))
            if seq > max_seq:
                max_seq = seq
    return max_seq + 1


def _build_spec_coverage_item(
    *,
    item_id: str,
    proposal: CheckProposal,
    generated_by: str,
    tier: int,
    baseline_pending: bool,
) -> HungerItem:
    """Build a ``H-SYN`` hunger item from an accepted proposal.

    The acceptance check type, params, and description are copied from
    the proposal.  ``generated_by`` is always the compiler-supplied
    value, never the nested ``proposed_by`` on the proposal, so worker-
    controlled proposal provenance cannot spoof ledger provenance.
    """
    check_params = dict(proposal.params)
    if proposal.fixture_argv is not None:
        check_params["fixture_argv"] = list(proposal.fixture_argv)
    return HungerItem(
        id=item_id,
        title=proposal.description or f"Spec coverage check ({item_id})",
        item_type=HungerItemType.GOAL_GAP,
        priority=1.0,
        gap_score=1.0,
        acceptance_checks=[
            AcceptanceCheck(
                check_type=proposal.check_type,
                params=check_params,
                description=proposal.description,
            )
        ],
        acceptance_mode="all",
        refinement_tier=tier,
        refinement_kind="spec_coverage",
        generated_by=generated_by,
        source_check_keys=[],
        synthesis_baseline_pending=baseline_pending,
        synthesis_fixture_argv=(
            list(proposal.fixture_argv)
            if proposal.fixture_argv is not None
            else None
        ),
        synthesis_prerequisite_check_keys=list(proposal.prerequisite_check_keys),
    )
