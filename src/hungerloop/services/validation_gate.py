"""Validation gate for HungerLoop v0.4.1.

:class:`ValidationGate` implements invariants I-5 (targeted validation: only
target items + regression checks run) and I-3 (check-level progress: the
:class:`~hungerloop.models.validation.ValidationReport` carries
``newly_passed_check_keys`` / ``regressed_check_keys`` / ``has_real_progress``).

The gate fetches the baseline :class:`~hungerloop.models.blackboard.BestState`,
identifies which checks to run (target items + previously-passed checks for
regression detection), dispatches each check via
:class:`~hungerloop.services.acceptance_runner.AcceptanceCheckRunner`, and
aggregates the results into a :class:`ValidationReport`.

Critical logic:
    - ``currently_passed`` is the union of newly-passed checks PLUS
      previously-passed checks that were not re-run (untested checks stay passed).
    - ``regressed_check_keys`` are checks that passed in the baseline but fail now.
    - Verdict: regressed→FAIL, missing_evidence→FAIL, all satisfied→PASS,
      some newly_passed→PARTIAL, else FAIL.
"""
from __future__ import annotations

from typing import Any

from hungerloop.models.enums import ValidationVerdict
from hungerloop.models.validation import CheckResult, ValidationReport
from hungerloop.services.acceptance_runner import AcceptanceCheckRunner


def make_check_key(hunger_item_id: str, check_index: int) -> str:
    """Build a check key from item ID and check index."""
    return f"{hunger_item_id}:{check_index}"


class ValidationGate:
    """Validate a candidate workspace against target hunger items."""

    def __init__(self, repo: Any, acceptance_runner: AcceptanceCheckRunner) -> None:
        # TODO(Task 14): tighten ``repo`` to the Repository protocol once it lands.
        self.repo = repo
        self.runner = acceptance_runner

    async def validate(
        self,
        task_id: str,
        loop_id: int,
        candidate: Any,
        target_hunger_item_ids: list[str],
    ) -> ValidationReport:
        """Validate a candidate against target items + regression checks.

        Args:
            task_id: Task identifier.
            loop_id: Loop iteration number.
            candidate: The :class:`~hungerloop.models.blackboard.CandidateState`.
            target_hunger_item_ids: Item IDs to validate (all checks run).

        Returns:
            A :class:`~hungerloop.models.validation.ValidationReport` with
            check-level progress signals.
        """
        best = self.repo.get_best_state(task_id)
        previously_passed = set(best.accepted_check_keys if best else [])

        target_items = self.repo.get_hunger_items(target_hunger_item_ids)

        regression_items = self.repo.get_items_for_check_keys(
            task_id=task_id,
            check_keys=list(previously_passed),
        )

        items_to_check = self._dedupe_items(target_items + regression_items)

        all_results: list[CheckResult] = []
        all_evidence_ids: list[str] = list(candidate.evidence_ids)

        for item in items_to_check:
            for idx, check in enumerate(item.acceptance_checks):
                check_key = make_check_key(item.id, idx)

                is_target = item.id in target_hunger_item_ids
                is_regression_check = check_key in previously_passed
                if not is_target and not is_regression_check:
                    continue

                passed, detail, ev_id = await self.runner.run(
                    check=check,
                    task_id=task_id,
                    loop_id=loop_id,
                    candidate=candidate,
                )

                previously = check_key in previously_passed
                newly = passed and not previously
                regressed = previously and not passed

                result = CheckResult(
                    hunger_item_id=item.id,
                    check_index=idx,
                    check_key=check_key,
                    check_type=check.check_type.value,
                    passed=passed,
                    previously_passed=previously,
                    newly_passed=newly,
                    regressed=regressed,
                    detail=detail,
                    evidence_id=ev_id,
                    workspace_ref=candidate.workspace_ref,
                )
                all_results.append(result)
                if ev_id:
                    all_evidence_ids.append(ev_id)

        currently_passed = {r.check_key for r in all_results if r.passed} | {
            key
            for key in previously_passed
            if key not in {r.check_key for r in all_results}
        }

        newly_passed = [r.check_key for r in all_results if r.newly_passed]
        regressed = [r.check_key for r in all_results if r.regressed]

        satisfied_items, unsatisfied_items = self._aggregate_item_satisfaction(
            target_items=target_items,
            check_results=all_results,
        )

        missing_evidence: list[str] = []
        if not all_evidence_ids:
            missing_evidence.append("Candidate produced no evidence.")

        has_real_progress = len(newly_passed) > 0

        verdict = self._decide_verdict(
            newly_passed_check_keys=newly_passed,
            regressed_check_keys=regressed,
            missing_evidence=missing_evidence,
            satisfied_hunger_item_ids=satisfied_items,
            unsatisfied_hunger_item_ids=unsatisfied_items,
        )

        return ValidationReport(
            id=f"VAL-{task_id}-{loop_id}",
            task_id=task_id,
            loop_id=loop_id,
            candidate_state_id=candidate.id,
            baseline_state_id=best.state_id if best else None,
            verdict=verdict,
            attempted_hunger_item_ids=target_hunger_item_ids,
            check_results=all_results,
            currently_passed_check_keys=sorted(currently_passed),
            newly_passed_check_keys=sorted(newly_passed),
            regressed_check_keys=sorted(regressed),
            satisfied_hunger_item_ids=satisfied_items,
            unsatisfied_hunger_item_ids=unsatisfied_items,
            evidence_ids=list(dict.fromkeys(all_evidence_ids)),
            missing_evidence=missing_evidence,
            regressions=[f"check:{k}:regressed" for k in regressed],
            recommended_next_actions=[
                f"Continue working on {iid}" for iid in unsatisfied_items[:3]
            ],
            has_real_progress=has_real_progress,
        )

    def _dedupe_items(self, items: list[Any]) -> list[Any]:
        """Deduplicate items by ID, preserving order."""
        seen: set[str] = set()
        out: list[Any] = []
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                out.append(item)
        return out

    def _aggregate_item_satisfaction(
        self,
        target_items: list[Any],
        check_results: list[CheckResult],
    ) -> tuple[list[str], list[str]]:
        """Compute satisfied/unsatisfied item IDs based on acceptance_mode."""
        by_item: dict[str, list[CheckResult]] = {}
        for r in check_results:
            by_item.setdefault(r.hunger_item_id, []).append(r)

        satisfied: list[str] = []
        unsatisfied: list[str] = []

        for item in target_items:
            results = by_item.get(item.id, [])
            if not results:
                unsatisfied.append(item.id)
                continue

            if item.acceptance_mode == "all":
                ok = all(r.passed for r in results)
            elif item.acceptance_mode == "any":
                ok = any(r.passed for r in results)
            else:
                ok = False

            if ok:
                satisfied.append(item.id)
            else:
                unsatisfied.append(item.id)

        return satisfied, unsatisfied

    def _decide_verdict(
        self,
        newly_passed_check_keys: list[str],
        regressed_check_keys: list[str],
        missing_evidence: list[str],
        satisfied_hunger_item_ids: list[str],
        unsatisfied_hunger_item_ids: list[str],
    ) -> ValidationVerdict:
        """Decide the validation verdict based on check outcomes."""
        if regressed_check_keys:
            return ValidationVerdict.FAIL

        if missing_evidence:
            return ValidationVerdict.FAIL

        if satisfied_hunger_item_ids and not unsatisfied_hunger_item_ids:
            return ValidationVerdict.PASS

        if newly_passed_check_keys:
            return ValidationVerdict.PARTIAL

        return ValidationVerdict.FAIL
