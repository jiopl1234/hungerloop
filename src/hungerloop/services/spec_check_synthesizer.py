"""Spec-to-check synthesis service for v0.7.

The :class:`SpecCheckSynthesizer` builds a prompt from mission prose and
feature descriptions, calls a completion client **outside**
``services/validators/``, wraps every LLM call with
:class:`CostGuard.assert_within_budget`, parses JSON proposal output
(including fenced JSON), drops malformed or disallowed items, gates
proposals with :class:`CheckProposalGate`, emits ``SYNTH_CHECK_REJECTED``
events for deterministic rejections or unparseable responses, and skips
completion calls when remaining synthesis capacity is zero.

Key invariants:

* **No LLM code in validators.** This module lives under ``services/``
  and never imports from ``services/validators/``.
* **Cost-guarded.** Every attempted LLM call has ``CostGuard`` before
  and after. Failure cases fail closed with non-secret audit events.
* **Compiler-owned injection.** Accepted proposals are returned to the
  caller; the caller routes them through
  ``RefinementCompiler.compile_spec_coverage``. The synthesizer never
  writes to the ledger directly.
* **Caps.** The synthesizer skips completion calls when remaining
  synthesis capacity is zero (``synthesis_max_total_items`` minus
  existing ``H-SYN`` items).
* **Source anchoring.** Accepted proposal ``source_quote`` values must
  be anchored to supplied spec text. Unanchored quotes are rejected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hungerloop.models.enums import AcceptanceCheckType, HungerItemStatus
from hungerloop.models.events import EventType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.check_proposal_gate import CheckProposalGate, GateResult
from hungerloop.services.completion_support import (
    CompletionClient,
    persist_completion_evidence,
)
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.llm_json import parse_json_response
from hungerloop.services.model_client import ModelCallError
from hungerloop.services.proposal_dedup import collect_rejected_proposal_dedup_keys
from hungerloop.services.refinement_compiler import (
    RefinementCompiler,
    _collect_existing_dedup_keys,
)
from hungerloop.services.spec_entailment_auditor import SpecEntailmentAuditor

SYNTHESIS_MAX_TOKENS = 65000

# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class SynthesisResult(BaseModel):
    """Result of one synthesis call."""

    accepted_proposals: list[CheckProposal] = Field(default_factory=list)
    rejected_count: int = 0
    skipped: bool = False
    skip_reason: str = ""
    completion_called: bool = False
    model_name: str = ""
    completion_evidence_id: str | None = None

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_proposals)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_synthesis_prompt(
    *,
    mission_prose: str,
    feature_descriptions: list[str],
    covered_check_digest: str | None = None,
) -> list[dict[str, str]]:
    """Build the LLM prompt from authoritative mission sources only.

    The system message instructs the model to return a JSON array of
    proposal objects. The user message supplies mission prose, feature
    descriptions, and (in incremental mode) a digest of already-covered
    checks so the model does not duplicate existing checks.

    Accepted proposal ``source_quote`` values must be anchored to text
    that appears in the supplied mission prose or feature descriptions.
    The synthesizer validates this after parsing.
    """
    system = (
        "You are a spec-to-check synthesis assistant. "
        "Analyze the mission spec and feature descriptions below. "
        "Propose deterministic acceptance checks as a JSON array. "
        "Each proposal must be a JSON object with keys: "
        '"check_type" (either "shell_exit_zero" or "file_exists"), '
        '"params" (for shell_exit_zero use {"argv": ["python", "-c", "..."]} '
        'as a JSON array of strings, never a single command string; '
        'for file_exists use {"path": "..."}), '
        '"description" (string), '
        '"source_quote" (exact quote from the spec that anchors this check), '
        '"fixture_argv" (for shell_exit_zero, a JSON array of strings that '
        'performs setup only and exits zero on the current compliant workspace), '
        'and "prerequisite_check_keys" (optional advisory JSON array of existing '
        'check keys). The fixture must not contain the assertion; params.argv '
        'must contain only the assertion and may assume fixture_argv just ran '
        'successfully in the same isolated workspace. '
        "Only use shell_exit_zero or file_exists. "
        "Do not include any markdown formatting. "
        "Return only the JSON array."
    )

    user_parts: list[str] = []
    user_parts.append(f"## Mission\n{mission_prose}")
    if feature_descriptions:
        user_parts.append("## Features")
        for desc in feature_descriptions:
            user_parts.append(f"- {desc}")
    if covered_check_digest:
        user_parts.append(f"## Already covered checks\n{covered_check_digest}")
    user_parts.append(
        "## Instructions\n"
        "Propose 0-5 deterministic acceptance checks as a JSON array. "
        "Each source_quote MUST be copied verbatim from the mission or "
        "feature text above."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_proposals(raw_text: str) -> list[dict[str, Any]]:
    """Parse raw model output into a list of proposal dicts.

    Handles:
    - Raw JSON array
    - Fenced JSON (```json ... ```)
    - JSON object wrapping an array (``{"proposals": [...]}``)
    - Non-JSON / garbage -> empty list
    - Non-list JSON -> empty list

    Never raises; returns an empty list on any parse failure.
    """
    parsed = parse_json_response(raw_text)
    if parsed is None:
        return []

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        # Could be ``{"proposals": [...]}`` or a single proposal
        proposals = parsed.get("proposals")
        if isinstance(proposals, list):
            return [item for item in proposals if isinstance(item, dict)]
        # Single proposal object
        return [parsed]
    return []


def _build_proposal_from_dict(
    raw: dict[str, Any],
    *,
    proposed_by: str = "synthesizer",
) -> CheckProposal | None:
    """Build a :class:`CheckProposal` from a parsed dict.

    Returns ``None`` if the dict is malformed or the proposal fails
    model validation. Never raises.
    """
    try:
        check_type_str = raw.get("check_type", "")
        if not isinstance(check_type_str, str):
            return None
        try:
            check_type = AcceptanceCheckType(check_type_str)
        except ValueError:
            return None

        params = raw.get("params")
        if not isinstance(params, dict):
            return None

        description = raw.get("description")
        if not isinstance(description, str):
            description = ""

        source_quote = raw.get("source_quote")
        if not isinstance(source_quote, str) or not source_quote.strip():
            return None

        fixture_argv = raw.get("fixture_argv")
        if fixture_argv is not None and not isinstance(fixture_argv, list):
            return None
        prerequisite_check_keys = raw.get("prerequisite_check_keys", [])
        if not isinstance(prerequisite_check_keys, list):
            return None

        return CheckProposal(
            check_type=check_type,
            params=params,
            description=description,
            source_quote=source_quote,
            proposed_by=proposed_by,
            fixture_argv=fixture_argv,
            prerequisite_check_keys=prerequisite_check_keys,
        )
    except Exception:
        return None


def _is_source_quote_anchored(
    quote: str,
    *,
    mission_prose: str,
    feature_descriptions: list[str],
) -> bool:
    """Check if a source quote appears in the supplied spec text.

    The check is case-sensitive but whitespace-normalized on both sides.
    This keeps anchoring strict while accepting quotes copied from YAML
    folded/literal text where newlines and indentation differ from the
    model's response.
    """
    trimmed = " ".join(quote.split())
    if not trimmed:
        return False
    combined = mission_prose + "\n" + "\n".join(feature_descriptions)
    return trimmed in " ".join(combined.split())


# ---------------------------------------------------------------------------
# Synthesis capacity helpers
# ---------------------------------------------------------------------------


def _count_synthesized_items(ledger_task_id: str, repo: RepositoryProtocol) -> int:
    """Count every ``H-SYN-*`` item ever created against the lifetime cap.

    ``synthesis_max_total_items`` is a monotonic kill-switch: it bounds how
    many synthesized objectives (and therefore how many synthesis LLM
    calls) a run will ever spend on. Resolved items — CLOSED (invalid
    synthesis) and VALIDATED_SATISFIED (auto-satisfied at baseline) — MUST
    still count. Excluding them lets capacity replenish every time a check
    resolves, so the synthesizer fires on every committed loop (bounded
    only by CostGuard) instead of hard-stopping at the cap.

    Freeing *concurrency* when items resolve is the job of the separate
    active-slot cap (:func:`_count_active_synthesized_items`), which does
    exclude resolved/BLOCKED items. The two caps are intentionally
    distinct: total = lifetime budget, active = concurrent backpressure.
    """
    ledger = repo.get_hunger_ledger(ledger_task_id)
    return sum(1 for item in ledger.items if item.id.startswith("H-SYN-"))


def _count_active_synthesized_items(
    ledger_task_id: str,
    repo: RepositoryProtocol,
) -> int:
    """Count unresolved synthesizer-owned items for backpressure.

    BLOCKED items are excluded: a never-pass synthesized item that the
    StagnationDetector blocks must not hold an active slot forever
    (spreadsheet-01 residual gap — the slot leak starved all later
    synthesis with ``synthesis_active_item_limit_reached``).
    """
    ledger = repo.get_hunger_ledger(ledger_task_id)
    non_occupying = {
        HungerItemStatus.CLOSED,
        HungerItemStatus.VALIDATED_SATISFIED,
        HungerItemStatus.BLOCKED,
    }
    return sum(
        1
        for item in ledger.items
        if item.generated_by == "synthesizer"
        and item.status not in non_occupying
        and item.gap_score > 0
    )


def _remaining_synthesis_capacity(
    *,
    task_id: str,
    repo: RepositoryProtocol,
    synthesis_max_total_items: int,
    synthesis_max_active_items: int | None = None,
) -> int:
    """Compute remaining synthesis capacity.

    Returns ``synthesis_max_total_items - count_of_existing_H_SYN_items``.
    Clamped to >= 0.
    """
    existing = _count_synthesized_items(task_id, repo)
    total_remaining = max(0, synthesis_max_total_items - existing)
    if synthesis_max_active_items is None:
        return total_remaining
    active = _count_active_synthesized_items(task_id, repo)
    active_remaining = max(0, synthesis_max_active_items - active)
    return min(total_remaining, active_remaining)


# ---------------------------------------------------------------------------
# SpecCheckSynthesizer
# ---------------------------------------------------------------------------


class SpecCheckSynthesizer:
    """Synthesize deterministic acceptance checks from mission specs.

    The synthesizer:
    1. Builds a prompt from mission prose and feature descriptions.
    2. Calls a completion client outside ``services/validators/``.
    3. Wraps every LLM call with ``CostGuard.assert_within_budget(task_id)``
       before and after.
    4. Parses JSON proposal output, including fenced JSON.
    5. Drops malformed or disallowed items.
    6. Gates proposals with :class:`CheckProposalGate`.
    7. Emits ``SYNTH_CHECK_REJECTED`` events for deterministic rejections
       or unparseable responses.
    8. Skips completion calls when remaining synthesis capacity is zero.
    """

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        cost_guard: CostGuard,
        completion_client: CompletionClient,
        gate: CheckProposalGate,
        model_name: str = "",
        auditor: SpecEntailmentAuditor | None = None,
    ) -> None:
        self.repo = repo
        self.cost_guard = cost_guard
        self.completion_client = completion_client
        self.gate = gate
        self.model_name = model_name
        self.auditor = auditor or SpecEntailmentAuditor(
            repo=repo,
            cost_guard=cost_guard,
            completion_client=completion_client,
            model_name=model_name,
        )

    async def synthesize(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        mission_prose: str,
        feature_descriptions: list[str],
        synthesis_max_total_items: int,
        synthesis_max_active_items: int | None = None,
        synthesis_batch_size: int | None = None,
        covered_check_digest: str | None = None,
        existing_dedup_keys: set[str] | None = None,
        dry_run_cwd: Path | None = None,
        require_fixture: bool = False,
        defer_fixture_precheck: bool = False,
        synthesis_audit_enabled: bool = False,
    ) -> SynthesisResult:
        """Run one synthesis pass.

        Args:
            task_id: Task identifier.
            loop_id: Current loop id (``None`` for plan-time).
            mission_prose: Mission description text.
            feature_descriptions: List of feature description strings.
            synthesis_max_total_items: Lifetime cap on synthesized items.
            covered_check_digest: Optional digest of already-covered checks
                for incremental mode.
            existing_dedup_keys: Optional pre-computed set of existing dedup
                keys. If not supplied, the synthesizer computes them from
                the ledger.
            dry_run_cwd: Optional candidate workspace root for gate dry-runs.

        Returns:
            :class:`SynthesisResult` with accepted proposals and metadata.
        """
        # Check capacity before any model call
        if synthesis_max_total_items <= 0:
            self._emit_skipped(
                task_id=task_id,
                loop_id=loop_id,
                reason="synthesis_max_total_items_is_zero",
            )
            return SynthesisResult(skipped=True, skip_reason="zero_capacity")

        remaining = _remaining_synthesis_capacity(
            task_id=task_id,
            repo=self.repo,
            synthesis_max_total_items=synthesis_max_total_items,
            synthesis_max_active_items=synthesis_max_active_items,
        )
        if remaining <= 0:
            total_remaining = max(
                0,
                synthesis_max_total_items
                - _count_synthesized_items(task_id, self.repo),
            )
            reason = (
                "synthesis_active_item_limit_reached"
                if total_remaining > 0 and synthesis_max_active_items is not None
                else "synthesis_capacity_exhausted"
            )
            self._emit_skipped(
                task_id=task_id,
                loop_id=loop_id,
                reason=reason,
            )
            return SynthesisResult(skipped=True, skip_reason=reason)

        ledger = self.repo.get_hunger_ledger(task_id)
        known_keys = _collect_existing_dedup_keys(ledger)
        if existing_dedup_keys is None:
            known_keys.update(
                collect_rejected_proposal_dedup_keys(self.repo, task_id)
            )
        else:
            known_keys.update(existing_dedup_keys)

        # Build prompt
        messages = build_synthesis_prompt(
            mission_prose=mission_prose,
            feature_descriptions=feature_descriptions,
            covered_check_digest=covered_check_digest,
        )

        # Emit synthesis-attempted event (non-secret)
        self.repo.append_event(
            EventType.SYNTHESIS_ATTEMPTED,
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "model": self.model_name,
                "remaining_capacity": remaining,
                "active_item_limit": synthesis_max_active_items,
                "batch_size": synthesis_batch_size,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

        # CostGuard BEFORE the call (I-8)
        self.cost_guard.assert_within_budget(task_id)

        raw_response = ""
        completion_evidence_id: str | None = None
        try:
            response = await self.completion_client.complete(
                messages=messages,
                max_tokens=SYNTHESIS_MAX_TOKENS,
            )
            raw_response = response.content
            completion_evidence_id = persist_completion_evidence(
                repo=self.repo,
                cost_guard=self.cost_guard,
                task_id=task_id,
                loop_id=loop_id,
                agent_id="spec_check_synthesizer",
                model_name=self.model_name,
                response=response,
            )
        except (ModelCallError, SafetyStopError):
            raise
        except Exception as exc:
            # CostGuard AFTER the call (I-8) — must run even on exception
            # paths where a request was attempted, so that cost ceilings
            # are enforced consistently.
            try:
                self.cost_guard.assert_within_budget(task_id)
            except SafetyStopError:
                raise
            # Fail closed: no accepted proposals, emit rejection event
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason=f"completion_error:{type(exc).__name__}",
                dedup_keys=[],
                details=[],
                raw_response=raw_response,
                evidence_id=completion_evidence_id,
            )
            return SynthesisResult(
                rejected_count=1,
                completion_called=True,
                model_name=self.model_name,
                completion_evidence_id=completion_evidence_id,
            )

        # CostGuard AFTER the call (I-8)
        try:
            self.cost_guard.assert_within_budget(task_id)
        except SafetyStopError:
            raise

        # Parse the response
        raw_proposals = _parse_proposals(raw_response)

        if not raw_proposals:
            # Empty or unparseable response
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason="unparseable_response",
                dedup_keys=[],
                details=[],
                raw_response=raw_response,
                evidence_id=completion_evidence_id,
            )
            return SynthesisResult(
                rejected_count=1,
                completion_called=True,
                model_name=self.model_name,
                completion_evidence_id=completion_evidence_id,
            )

        # Build CheckProposal objects, validating and anchoring source quotes
        valid_proposals: list[CheckProposal] = []
        rejected_keys: list[str] = []
        rejection_details: list[dict[str, object]] = []
        for raw in raw_proposals:
            proposal = _build_proposal_from_dict(raw, proposed_by="synthesizer")
            if proposal is None:
                rejected_keys.append("invalid_proposal_shape")
                rejection_details.append(
                    {
                        "reason": "invalid_proposal_shape",
                        "source_quote": raw.get("source_quote") if isinstance(raw, dict) else None,
                        "raw": raw,
                    }
                )
                continue

            # Check source quote anchoring
            if not _is_source_quote_anchored(
                proposal.source_quote,
                mission_prose=mission_prose,
                feature_descriptions=feature_descriptions,
            ):
                key = proposal.dedup_key()
                rejected_keys.append(key)
                rejection_details.append(
                    {
                        "reason": "source_quote_unanchored",
                        "dedup_key": key,
                        "check_type": proposal.check_type.value,
                        "source_quote": proposal.source_quote,
                        "description": proposal.description,
                    }
                )
                continue

            valid_proposals.append(proposal)

        if not valid_proposals:
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason="no_valid_proposals",
                dedup_keys=rejected_keys,
                details=rejection_details,
                raw_response=raw_response,
                evidence_id=completion_evidence_id,
            )
            return SynthesisResult(
                rejected_count=len(rejected_keys),
                completion_called=True,
                model_name=self.model_name,
                completion_evidence_id=completion_evidence_id,
            )

        # Gate the proposals
        gate_result: GateResult = await self.gate.filter(
            valid_proposals,
            existing_keys=known_keys,
            dry_run_cwd=dry_run_cwd,
            require_fixture=require_fixture,
            defer_fixture_precheck=defer_fixture_precheck,
        )

        # Emit rejection events for gate-rejected proposals
        if gate_result.rejected:
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason="gate_rejection",
                dedup_keys=[r.dedup_key for r in gate_result.rejected],
                details=[
                    {
                        "reason": r.reason,
                        "dedup_key": r.dedup_key,
                        "check_type": r.proposal.check_type.value,
                        "source_quote": r.proposal.source_quote,
                        "description": r.proposal.description,
                    }
                    for r in gate_result.rejected
                ],
                raw_response=raw_response,
                evidence_id=completion_evidence_id,
            )

        audited_proposals = gate_result.accepted
        audit_rejected_count = 0
        if synthesis_audit_enabled and audited_proposals:
            audit_result = await self.auditor.audit(
                task_id=task_id,
                loop_id=loop_id,
                mission_prose=mission_prose,
                feature_descriptions=feature_descriptions,
                proposals=audited_proposals,
            )
            audited_proposals = audit_result.accepted
            audit_rejected_count = len(audit_result.rejected)
            if audit_result.rejected:
                self._emit_rejection(
                    task_id=task_id,
                    loop_id=loop_id,
                    reason="semantic_audit_rejection",
                    dedup_keys=[
                        entry.proposal.dedup_key()
                        for entry in audit_result.rejected
                    ],
                    details=[
                        {
                            "reason": entry.reason,
                            "dedup_key": entry.proposal.dedup_key(),
                            "check_type": entry.proposal.check_type.value,
                            "source_quote": entry.proposal.source_quote,
                            "description": entry.proposal.description,
                        }
                        for entry in audit_result.rejected
                    ],
                    evidence_id=completion_evidence_id,
                )

        # Cap accepted proposals at remaining capacity
        accepted_limit = remaining
        if synthesis_batch_size is not None:
            accepted_limit = min(accepted_limit, max(0, synthesis_batch_size))
        accepted = audited_proposals[:accepted_limit]

        return SynthesisResult(
            accepted_proposals=accepted,
            rejected_count=(
                len(rejected_keys)
                + len(gate_result.rejected)
                + audit_rejected_count
            ),
            completion_called=True,
            model_name=self.model_name,
            completion_evidence_id=completion_evidence_id,
        )

    def _emit_rejection(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        reason: str,
        dedup_keys: list[str],
        details: list[dict[str, object]] | None = None,
        raw_response: str | None = None,
        evidence_id: str | None = None,
    ) -> None:
        """Emit a ``SYNTH_CHECK_REJECTED`` event with diagnostic payload."""
        payload: dict[str, object] = {
            "task_id": task_id,
            "loop_id": loop_id,
            "reason": reason,
            "rejected_count": len(dedup_keys),
            "dedup_keys": list(dedup_keys),
        }
        if details is not None:
            payload["rejected_proposals"] = details
        if raw_response is not None:
            payload["raw_response_excerpt"] = raw_response[:4000]
        if evidence_id is not None:
            payload["evidence_id"] = evidence_id
        self.repo.append_event(
            EventType.SYNTH_CHECK_REJECTED,
            payload,
            task_id=task_id,
            loop_id=loop_id,
        )

    def _emit_skipped(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        reason: str,
    ) -> None:
        """Emit a ``SYNTHESIS_SKIPPED`` event with non-secret payload."""
        self.repo.append_event(
            EventType.SYNTHESIS_SKIPPED,
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "reason": reason,
            },
            task_id=task_id,
            loop_id=loop_id,
        )

    async def synthesize_post_commit(
        self,
        *,
        task_id: str,
        loop_id: int,
        mission_prose: str,
        feature_descriptions: list[str],
        synthesis_max_total_items: int,
        covered_check_digest: str | None,
        synthesis_max_active_items: int | None = None,
        synthesis_batch_size: int | None = None,
        synthesis_audit_enabled: bool = False,
        dry_run_cwd: Path | None = None,
        existing_dedup_keys: set[str] | None = None,
    ) -> list[str]:
        """Run post-commit synthesis and inject through the compiler.

        This method is the orchestrator-facing entry point. It runs
        synthesis and routes accepted proposals through
        ``RefinementCompiler.compile_spec_coverage``.

        Returns the list of injected item ids.
        """
        result = await self.synthesize(
            task_id=task_id,
            loop_id=loop_id,
            mission_prose=mission_prose,
            feature_descriptions=feature_descriptions,
            synthesis_max_total_items=synthesis_max_total_items,
            synthesis_max_active_items=synthesis_max_active_items,
            synthesis_batch_size=synthesis_batch_size,
            covered_check_digest=covered_check_digest,
            existing_dedup_keys=existing_dedup_keys,
            dry_run_cwd=dry_run_cwd,
            require_fixture=True,
            synthesis_audit_enabled=synthesis_audit_enabled,
        )

        if not result.accepted_proposals:
            return []

        compiler = RefinementCompiler(self.repo)
        return compiler.compile_spec_coverage(
            task_id=task_id,
            proposals=result.accepted_proposals,
            generated_by="synthesizer",
            tier=1,
            max_new_items=synthesis_max_total_items,
            baseline_pending=True,
        )


# ---------------------------------------------------------------------------
# Plan-time synthesis helper
# ---------------------------------------------------------------------------


async def run_plan_time_synthesis(
    *,
    task_id: str,
    repo: RepositoryProtocol,
    cost_guard: CostGuard,
    completion_client: CompletionClient,
    gate: CheckProposalGate,
    refinement_compiler: RefinementCompiler,
    mission_prose: str,
    feature_descriptions: list[str],
    synthesis_plan_time_tier: int,
    synthesis_max_total_items: int,
    synthesis_max_active_items: int = 3,
    synthesis_batch_size: int = 3,
    synthesis_audit_enabled: bool = False,
    model_name: str = "",
) -> list[str]:
    """Run plan-time synthesis and inject accepted proposals.

    This is the entry point called by ``mission new`` / ``mission import``
    when ``synthesis_enabled=True``. It constructs a
    :class:`SpecCheckSynthesizer`, runs synthesis at loop_id=None (plan-
    time), and routes accepted proposals through
    ``RefinementCompiler.compile_spec_coverage``.

    Returns the list of injected item ids.
    """
    synthesizer = SpecCheckSynthesizer(
        repo=repo,
        cost_guard=cost_guard,
        completion_client=completion_client,
        gate=gate,
        model_name=model_name,
    )

    result = await synthesizer.synthesize(
        task_id=task_id,
        loop_id=None,
        mission_prose=mission_prose,
        feature_descriptions=feature_descriptions,
        synthesis_max_total_items=synthesis_max_total_items,
        synthesis_max_active_items=synthesis_max_active_items,
        synthesis_batch_size=synthesis_batch_size,
        require_fixture=True,
        defer_fixture_precheck=True,
        synthesis_audit_enabled=synthesis_audit_enabled,
    )

    if not result.accepted_proposals:
        return []

    injected_ids = refinement_compiler.compile_spec_coverage(
        task_id=task_id,
        proposals=result.accepted_proposals,
        generated_by="synthesizer",
        tier=synthesis_plan_time_tier,
        max_new_items=synthesis_max_total_items,
        baseline_pending=True,
    )
    return injected_ids


# ---------------------------------------------------------------------------
# Post-commit synthesis helper
# ---------------------------------------------------------------------------


async def run_post_commit_synthesis(
    *,
    task_id: str,
    loop_id: int,
    repo: RepositoryProtocol,
    cost_guard: CostGuard,
    completion_client: CompletionClient,
    gate: CheckProposalGate,
    refinement_compiler: RefinementCompiler,
    mission_prose: str,
    feature_descriptions: list[str],
    synthesis_max_total_items: int,
    synthesis_max_active_items: int = 3,
    synthesis_batch_size: int = 3,
    synthesis_audit_enabled: bool = False,
    covered_check_digest: str | None = None,
    model_name: str = "",
    dry_run_cwd: Path | None = None,
) -> list[str]:
    """Run post-commit incremental synthesis and inject accepted proposals.

    This is the entry point called by the orchestrator after a successful
    commit. It constructs a :class:`SpecCheckSynthesizer`, runs synthesis
    with the current loop_id, and routes accepted proposals through
    ``RefinementCompiler.compile_spec_coverage``.

    Returns the list of injected item ids.
    """
    synthesizer = SpecCheckSynthesizer(
        repo=repo,
        cost_guard=cost_guard,
        completion_client=completion_client,
        gate=gate,
        model_name=model_name,
    )

    result = await synthesizer.synthesize(
        task_id=task_id,
        loop_id=loop_id,
        mission_prose=mission_prose,
        feature_descriptions=feature_descriptions,
        synthesis_max_total_items=synthesis_max_total_items,
        synthesis_max_active_items=synthesis_max_active_items,
        synthesis_batch_size=synthesis_batch_size,
        covered_check_digest=covered_check_digest,
        dry_run_cwd=dry_run_cwd,
        require_fixture=True,
        synthesis_audit_enabled=synthesis_audit_enabled,
    )

    if not result.accepted_proposals:
        return []

    injected_ids = refinement_compiler.compile_spec_coverage(
        task_id=task_id,
        proposals=result.accepted_proposals,
        generated_by="synthesizer",
        tier=1,
        max_new_items=synthesis_max_total_items,
        baseline_pending=True,
    )
    return injected_ids


# ---------------------------------------------------------------------------
# Covered-check digest builder
# ---------------------------------------------------------------------------


def build_covered_check_digest(
    *,
    repo: RepositoryProtocol,
    task_id: str,
    rejected_dedup_keys: set[str] | None = None,
) -> str:
    """Build a digest of already-covered operator and synthesized checks.

    Used in incremental (post-commit) mode to supply the synthesizer
    with context about existing checks so it does not propose
    duplicates.
    """
    ledger = repo.get_hunger_ledger(task_id)
    lines: list[str] = []
    for item in ledger.items:
        for check in item.acceptance_checks:
            lines.append(
                f"- {item.id}: {check.check_type.value} "
                f"({check.description or 'no description'})"
            )
    rejected_keys = (
        collect_rejected_proposal_dedup_keys(repo, task_id)
        if rejected_dedup_keys is None
        else rejected_dedup_keys
    )
    for dedup_key in sorted(rejected_keys):
        lines.append(f"- rejected proposal: {dedup_key} (do not propose again)")
    return "\n".join(lines) if lines else "(no existing checks)"
