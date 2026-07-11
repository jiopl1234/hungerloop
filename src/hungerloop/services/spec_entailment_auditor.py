"""Optional fail-open semantic audit for synthesized acceptance checks."""
from __future__ import annotations

import json
from dataclasses import dataclass

from hungerloop.models.events import EventType
from hungerloop.models.synthesis import CheckProposal
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.completion_support import (
    CompletionClient,
    persist_completion_evidence,
)
from hungerloop.services.cost_guard import CostGuard, SafetyStopError
from hungerloop.services.llm_json import parse_json_response

AUDIT_MAX_TOKENS = 65000


@dataclass(frozen=True)
class AuditRejection:
    proposal: CheckProposal
    reason: str


@dataclass(frozen=True)
class EntailmentAuditResult:
    accepted: list[CheckProposal]
    rejected: list[AuditRejection]
    failed_open: bool = False


class SpecEntailmentAuditor:
    """Reject only proposals explicitly judged unsupported by the spec."""

    def __init__(
        self,
        *,
        repo: RepositoryProtocol,
        cost_guard: CostGuard,
        completion_client: CompletionClient,
        model_name: str = "",
    ) -> None:
        self.repo = repo
        self.cost_guard = cost_guard
        self.completion_client = completion_client
        self.model_name = model_name

    async def audit(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        mission_prose: str,
        feature_descriptions: list[str],
        proposals: list[CheckProposal],
    ) -> EntailmentAuditResult:
        if not proposals:
            return EntailmentAuditResult([], [])

        messages = _build_audit_prompt(
            mission_prose=mission_prose,
            feature_descriptions=feature_descriptions,
            proposals=proposals,
        )
        self.cost_guard.assert_within_budget(task_id)
        raw_response = ""
        evidence_id: str | None = None
        try:
            response = await self.completion_client.complete(
                messages=messages,
                max_tokens=AUDIT_MAX_TOKENS,
            )
            raw_response = response.content
            evidence_id = persist_completion_evidence(
                repo=self.repo,
                cost_guard=self.cost_guard,
                task_id=task_id,
                loop_id=loop_id,
                agent_id="spec_entailment_auditor",
                model_name=self.model_name,
                response=response,
            )
        except SafetyStopError:
            raise
        except Exception as exc:
            self.cost_guard.assert_within_budget(task_id)
            self._emit_failed_open(
                task_id=task_id,
                loop_id=loop_id,
                reason=f"completion_error:{type(exc).__name__}",
                raw_response=raw_response,
            )
            return EntailmentAuditResult(list(proposals), [], failed_open=True)
        self.cost_guard.assert_within_budget(task_id)

        decisions = _parse_audit_decisions(raw_response)
        if decisions is None:
            self._emit_failed_open(
                task_id=task_id,
                loop_id=loop_id,
                reason="unparseable_response",
                raw_response=raw_response,
                evidence_id=evidence_id,
            )
            return EntailmentAuditResult(list(proposals), [], failed_open=True)

        accepted: list[CheckProposal] = []
        rejected: list[AuditRejection] = []
        for proposal in proposals:
            decision = decisions.get(proposal.dedup_key())
            if decision is None or decision[0]:
                accepted.append(proposal)
                continue
            rejected.append(AuditRejection(proposal, decision[1]))

        if rejected:
            self.repo.append_event(
                EventType.SYNTH_CHECK_AUDIT_REJECTED,
                {
                    "model": self.model_name,
                    "rejected_proposals": [
                        {
                            "dedup_key": entry.proposal.dedup_key(),
                            "source_quote": entry.proposal.source_quote,
                            "reason": entry.reason,
                        }
                        for entry in rejected
                    ],
                    "raw_response_excerpt": raw_response[:4000],
                    "evidence_id": evidence_id,
                },
                task_id=task_id,
                loop_id=loop_id,
            )
        return EntailmentAuditResult(accepted, rejected)

    def _emit_failed_open(
        self,
        *,
        task_id: str,
        loop_id: int | None,
        reason: str,
        raw_response: str,
        evidence_id: str | None = None,
    ) -> None:
        self.repo.append_event(
            EventType.SYNTH_AUDIT_FAILED_OPEN,
            {
                "model": self.model_name,
                "reason": reason,
                "raw_response_excerpt": raw_response[:4000],
                "evidence_id": evidence_id,
            },
            task_id=task_id,
            loop_id=loop_id,
        )


def _build_audit_prompt(
    *,
    mission_prose: str,
    feature_descriptions: list[str],
    proposals: list[CheckProposal],
) -> list[dict[str, str]]:
    proposal_payload = [
        {
            "dedup_key": proposal.dedup_key(),
            "check_type": proposal.check_type.value,
            "params": proposal.params,
            "description": proposal.description,
            "source_quote": proposal.source_quote,
            "prerequisite_check_keys": proposal.prerequisite_check_keys,
        }
        for proposal in proposals
    ]
    system = (
        "Audit whether each proposed assertion is directly entailed by the supplied "
        "spec. Do not judge fixture validity or current implementation behavior. "
        "Return JSON only: {\"decisions\":[{\"dedup_key\":str,"
        "\"entailed\":bool,\"reason\":str}]}"
    )
    user = (
        f"Mission:\n{mission_prose}\n\nFeatures:\n"
        + "\n".join(feature_descriptions)
        + "\n\nProposals:\n"
        + json.dumps(proposal_payload, ensure_ascii=True, sort_keys=True)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_audit_decisions(raw_response: str) -> dict[str, tuple[bool, str]] | None:
    payload = parse_json_response(raw_response)
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        return None

    decisions: dict[str, tuple[bool, str]] = {}
    for raw in payload["decisions"]:
        if not isinstance(raw, dict):
            continue
        dedup_key = raw.get("dedup_key")
        entailed = raw.get("entailed")
        reason = raw.get("reason", "not entailed by spec")
        if (
            isinstance(dedup_key, str)
            and isinstance(entailed, bool)
            and isinstance(reason, str)
        ):
            decisions[dedup_key] = (entailed, reason)
    return decisions
