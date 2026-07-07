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

import json
import re
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from hungerloop.models.enums import AcceptanceCheckType
from hungerloop.models.events import EventType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.check_proposal_gate import CheckProposalGate, GateResult
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.model_client import ModelCallError, ModelResponse
from hungerloop.services.refinement_compiler import (
    RefinementCompiler,
    _collect_existing_dedup_keys,
)

# ---------------------------------------------------------------------------
# Completion client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CompletionClient(Protocol):
    """Minimal completion-client boundary used by the synthesizer.

    This is intentionally narrower than :class:`ModelClient` so the
    synthesizer can be tested with a lightweight fake without pulling in
    the full worker-runtime stack. Production wiring uses
    :class:`OpenAIModelClient` (or a configured real client).
    """

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ModelResponse: ...


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
        '"params" (dict with "argv" for shell or "path" for file), '
        '"description" (string), '
        '"source_quote" (exact quote from the spec that anchors this check). '
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


def _strip_fenced_json(text: str) -> str:
    """Extract JSON from fenced code blocks (```json ... ```).

    If the text is not fenced, return it as-is.
    """
    # Match ```json ... ``` or ``` ... ```
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)```",
        text,
        re.DOTALL,
    )
    if fence_match:
        return fence_match.group(1).strip()
    return text.strip()


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
    stripped = _strip_fenced_json(raw_text)
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
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

        return CheckProposal(
            check_type=check_type,
            params=params,
            description=description,
            source_quote=source_quote,
            proposed_by=proposed_by,
        )
    except Exception:
        return None


def _is_source_quote_anchored(
    quote: str,
    *,
    mission_prose: str,
    feature_descriptions: list[str],
) -> bool:
    """Check if a source quote appears verbatim in the supplied spec text.

    The check is case-sensitive and trims surrounding whitespace from
    the quote before looking for a substring match.
    """
    trimmed = quote.strip()
    if not trimmed:
        return False
    combined = mission_prose + "\n" + "\n".join(feature_descriptions)
    return trimmed in combined


# ---------------------------------------------------------------------------
# Synthesis capacity helpers
# ---------------------------------------------------------------------------


def _count_synthesized_items(ledger_task_id: str, repo: RepositoryProtocol) -> int:
    """Count existing ``H-SYN-*`` items in the ledger for a task."""
    ledger = repo.get_hunger_ledger(ledger_task_id)
    return sum(1 for item in ledger.items if item.id.startswith("H-SYN-"))


def _remaining_synthesis_capacity(
    *,
    task_id: str,
    repo: RepositoryProtocol,
    synthesis_max_total_items: int,
) -> int:
    """Compute remaining synthesis capacity.

    Returns ``synthesis_max_total_items - count_of_existing_H_SYN_items``.
    Clamped to >= 0.
    """
    existing = _count_synthesized_items(task_id, repo)
    return max(0, synthesis_max_total_items - existing)


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
    ) -> None:
        self.repo = repo
        self.cost_guard = cost_guard
        self.completion_client = completion_client
        self.gate = gate
        self.model_name = model_name

    async def synthesize(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        mission_prose: str,
        feature_descriptions: list[str],
        synthesis_max_total_items: int,
        covered_check_digest: str | None = None,
        existing_dedup_keys: set[str] | None = None,
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
        )
        if remaining <= 0:
            self._emit_skipped(
                task_id=task_id,
                loop_id=loop_id,
                reason="synthesis_capacity_exhausted",
            )
            return SynthesisResult(skipped=True, skip_reason="capacity_exhausted")

        # Compute existing dedup keys if not supplied
        if existing_dedup_keys is None:
            ledger = self.repo.get_hunger_ledger(task_id)
            existing_dedup_keys = _collect_existing_dedup_keys(ledger)

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
            },
            task_id=task_id,
            loop_id=loop_id,
        )

        # CostGuard BEFORE the call (I-8)
        self.cost_guard.assert_within_budget(task_id)

        # Make the completion call
        raw_response: str
        try:
            response = await self.completion_client.complete(
                messages=messages,
                max_tokens=1024,
            )
            raw_response = response.content
        except (ModelCallError, SafetyStopError):
            raise
        except Exception as exc:
            # Fail closed: no accepted proposals, emit rejection event
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason=f"completion_error:{type(exc).__name__}",
                dedup_keys=[],
            )
            return SynthesisResult(
                rejected_count=1,
                completion_called=True,
                model_name=self.model_name,
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
            )
            return SynthesisResult(
                rejected_count=1,
                completion_called=True,
                model_name=self.model_name,
            )

        # Build CheckProposal objects, validating and anchoring source quotes
        valid_proposals: list[CheckProposal] = []
        rejected_keys: list[str] = []
        for raw in raw_proposals:
            proposal = _build_proposal_from_dict(raw, proposed_by="synthesizer")
            if proposal is None:
                rejected_keys.append("invalid_proposal_shape")
                continue

            # Check source quote anchoring
            if not _is_source_quote_anchored(
                proposal.source_quote,
                mission_prose=mission_prose,
                feature_descriptions=feature_descriptions,
            ):
                rejected_keys.append(proposal.dedup_key())
                continue

            valid_proposals.append(proposal)

        if not valid_proposals:
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason="no_valid_proposals",
                dedup_keys=rejected_keys,
            )
            return SynthesisResult(
                rejected_count=len(rejected_keys),
                completion_called=True,
                model_name=self.model_name,
            )

        # Gate the proposals
        gate_result: GateResult = await self.gate.filter(
            valid_proposals,
            existing_keys=existing_dedup_keys,
        )

        # Emit rejection events for gate-rejected proposals
        if gate_result.rejected:
            self._emit_rejection(
                task_id=task_id,
                loop_id=loop_id,
                reason="gate_rejection",
                dedup_keys=[r.dedup_key for r in gate_result.rejected],
            )

        # Cap accepted proposals at remaining capacity
        accepted = gate_result.accepted[:remaining]

        return SynthesisResult(
            accepted_proposals=accepted,
            rejected_count=len(gate_result.rejected)
            + (len(valid_proposals) - len(gate_result.accepted)),
            completion_called=True,
            model_name=self.model_name,
        )

    def _emit_rejection(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        reason: str,
        dedup_keys: list[str],
    ) -> None:
        """Emit a ``SYNTH_CHECK_REJECTED`` event with non-secret payload."""
        self.repo.append_event(
            EventType.SYNTH_CHECK_REJECTED,
            {
                "task_id": task_id,
                "loop_id": loop_id,
                "reason": reason,
                "rejected_count": len(dedup_keys),
                "dedup_keys": list(dedup_keys),
            },
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
            covered_check_digest=covered_check_digest,
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
    )

    if not result.accepted_proposals:
        return []

    injected_ids = refinement_compiler.compile_spec_coverage(
        task_id=task_id,
        proposals=result.accepted_proposals,
        generated_by="synthesizer",
        tier=synthesis_plan_time_tier,
        max_new_items=synthesis_max_total_items,
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
    covered_check_digest: str | None = None,
    model_name: str = "",
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
        covered_check_digest=covered_check_digest,
    )

    if not result.accepted_proposals:
        return []

    injected_ids = refinement_compiler.compile_spec_coverage(
        task_id=task_id,
        proposals=result.accepted_proposals,
        generated_by="synthesizer",
        tier=1,
        max_new_items=synthesis_max_total_items,
    )
    return injected_ids


# ---------------------------------------------------------------------------
# Covered-check digest builder
# ---------------------------------------------------------------------------


def build_covered_check_digest(
    *,
    repo: RepositoryProtocol,
    task_id: str,
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
    return "\n".join(lines) if lines else "(no existing checks)"
