"""Deterministic refinement-tier hunger item generation.

v0.5f.4 keeps requirement generation rule-based: once tier-0 correctness
is done, this compiler can add concrete, check-backed refinement items
while the user-provided loop budget remains.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from hungerloop.models.blackboard import BestState
from hungerloop.models.enums import AcceptanceCheckType, CompletionMode, HungerItemType
from hungerloop.models.hunger import (
    AcceptanceCheck,
    HungerItem,
    HungerLedger,
    HungerPolicy,
)
from hungerloop.repository.protocol import RepositoryProtocol

_PROFILE_PYTHON_MEDIUM = "python_medium"


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
