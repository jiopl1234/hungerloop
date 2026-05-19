"""Context builder service for HungerLoop v0.4.1.

:class:`ContextBuilder` constructs agent execution contexts from repository state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from hungerloop.models.context import ContextPack, TruncationInfo
from hungerloop.models.enums import CompletionMode, EvidenceType
from hungerloop.models.hunger import AcceptanceCheck
from hungerloop.models.planning import BudgetAllocation
from hungerloop.repository.evidence_success import is_successful_evidence_payload
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.evidence_render import (
    summarize_failed_check,
    summarize_tool_call,
)
from hungerloop.services.prior_loop_context import render_prior_loop_context_block
from hungerloop.services.workspace_reader import WorkspaceReader

K_REJECT_WINDOW = 3
K_EVIDENCE_WINDOW = 2
MAX_LINE_CHARS = 500
MAX_BEST_SUMMARY_CHARS = 800
MAX_LAST_SELF_SUMMARY_CHARS = 200
MAX_WORKSPACE_FILE_PATH_CHARS = 120
# Keep the non-evictable rendered prior-loop block under MAX_HISTORY_CHARS:
# last summary + best summary + best-files line + static labels/hints.
MAX_WORKSPACE_FILES_LINE_CHARS = 700
# The cap relies on the non-evictable section caps above; _apply_history_cap
# asserts if those caps drift past the rendered prior-loop block budget.
MAX_HISTORY_CHARS = 2000
READ_ONLY_REJECTED_HINT = (
    "two recent rejected loops only read files; next attempt must patch/write "
    "or declare a blocker"
)


def _format_check(check: AcceptanceCheck) -> str:
    """Render one acceptance check as a single line carrying both the
    human description and the machine-checkable params.

    The params field contains the actual semantics the validator will
    enforce (file paths, argv assertions, etc.). Without them the worker
    only sees a description like "pytest passes" and has to guess what
    the test actually checks. With them the worker can read e.g.
    ``argv=['python','-c','assert fizzbuzz(3)=="Fizz"']`` and infer the
    rule directly.
    """
    desc = (check.description or check.check_type.value).strip()
    try:
        params_blob = json.dumps(check.params, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        params_blob = str(check.params)
    return f"{desc} [{check.check_type.value} params={params_blob}]"


@dataclass(frozen=True)
class _LoopHistorySlice:
    rejected_lines: list[str]
    successful_evidence_ids: list[str]
    successful_evidence_lines: list[str]
    last_self_summary: str | None


class ContextBuilder:
    """Build agent execution contexts."""

    def __init__(
        self,
        repo: RepositoryProtocol,
        workspace_reader: WorkspaceReader,
    ) -> None:
        self.repo = repo
        self.workspace_reader = workspace_reader

    def build_for_agent(
        self,
        task_id: str,
        loop_id: int,
        agent_id: str,
        mission: str,
        target_hunger_item_ids: list[str],
        budget: BudgetAllocation,
        allowed_tools: list[str],
        output_schema_name: str,
        candidate_workspace_ref: str,
    ) -> ContextPack:
        """Build a context pack for an agent.

        Args:
            task_id: Task identifier.
            loop_id: Loop iteration.
            agent_id: Agent identifier.
            mission: Agent's mission description.
            target_hunger_item_ids: Hunger items to work on.
            budget: Budget allocation for this phase.
            allowed_tools: Tools the agent can use.
            output_schema_name: Required output schema.
            candidate_workspace_ref: Workspace reference for the candidate.

        Returns:
            A context pack for the agent.
        """
        best = self.repo.get_best_state(task_id)
        best_summary, best_summary_truncated = _clip_optional(
            best.summary if best else None,
            MAX_BEST_SUMMARY_CHARS,
        )
        history = self._loop_history(task_id, agent_id, loop_id)
        handoff_result = self.repo.get_latest_handoff_processing_result(task_id)
        last_summary, _ = _clip_optional(
            history.last_self_summary,
            MAX_LAST_SELF_SUMMARY_CHARS,
        )
        prior_handoff_summary = (
            handoff_result.prior_handoff_summary if handoff_result else ""
        )
        best_files = _shape_workspace_files(
            self.workspace_reader.list_workspace_files(task_id, ref="best"),
            path_cap=MAX_WORKSPACE_FILE_PATH_CHARS,
            line_cap=MAX_WORKSPACE_FILES_LINE_CHARS,
            max_paths=20,
        )
        # Evidence slots are global recency slots, not per-loop quotas: a busy
        # newest committed loop can occupy all slots before older loops appear.
        evidence_ids = history.successful_evidence_ids[: K_EVIDENCE_WINDOW * 5]
        evidence_lines = history.successful_evidence_lines[: K_EVIDENCE_WINDOW * 5]
        failure_lines = history.rejected_lines[: K_REJECT_WINDOW * 4]
        if self._should_emit_read_only_rejected_hint(task_id, loop_id):
            failure_lines.insert(0, READ_ONLY_REJECTED_HINT)
        (
            prior_handoff_summary,
            last_summary,
            evidence_ids,
            evidence_lines,
            failure_lines,
            truncation_info,
        ) = (
            _apply_history_cap(
                loop_id=loop_id,
                prior_handoff_summary=prior_handoff_summary,
                last_summary=last_summary,
                best_summary=best_summary,
                best_files=best_files,
                evidence_ids=evidence_ids,
                evidence_lines=evidence_lines,
                failure_lines=failure_lines,
                best_summary_truncated=best_summary_truncated,
            )
        )

        items = self.repo.get_hunger_items(target_hunger_item_ids)
        acceptance_criteria = []
        for item in items:
            for check in item.acceptance_checks:
                acceptance_criteria.append(_format_check(check))

        return ContextPack(
            task_id=task_id,
            loop_id=loop_id,
            agent_id=agent_id,
            mission=mission,
            phase=budget.phase.value,
            target_hunger_item_ids=target_hunger_item_ids,
            acceptance_criteria=acceptance_criteria,
            best_state_summary=best_summary,
            candidate_workspace_ref=candidate_workspace_ref,
            relevant_evidence_ids=evidence_ids,
            failure_patterns_to_avoid=failure_lines,
            last_self_summary=last_summary,
            prior_handoff_summary=prior_handoff_summary,
            relevant_evidence_summaries=evidence_lines,
            best_workspace_files=best_files,
            truncation_info=truncation_info,
            allowed_tools=allowed_tools,
            budget=budget,
            required_output_schema=output_schema_name,
        )

    def _loop_history(
        self,
        task_id: str,
        agent_id: str,
        current_loop_id: int,
    ) -> _LoopHistorySlice:
        result = self.repo.get_last_worker_result(task_id, agent_id, current_loop_id)
        last_summary = result.summary if result and result.summary else None
        traces = self.repo.list_loop_traces(task_id)

        rejected_lines: list[str] = []
        rejected = [
            trace
            for trace in traces
            if not trace.committed and trace.loop_id < current_loop_id
        ]
        rejected.sort(key=lambda trace: trace.loop_id, reverse=True)
        for trace in rejected[:K_REJECT_WINDOW]:
            if trace.validation_report_id is None:
                continue
            report = self.repo.get_validation_report(trace.validation_report_id)
            if report is None:
                continue
            for check in report.check_results:
                if not check.passed:
                    rejected_lines.append(
                        summarize_failed_check(
                            check,
                            trace.loop_id,
                            max_chars=MAX_LINE_CHARS,
                        )
                    )

        committed_loop_ids = {trace.loop_id for trace in traces if trace.committed}
        rejected_loop_ids = {
            trace.loop_id for trace in traces if not trace.committed
        }
        min_loop_id = current_loop_id - K_EVIDENCE_WINDOW
        evidence_rows: list[tuple[int, int, str, str]] = []
        for index, row in enumerate(
            self.repo.list_successful_tool_call_evidence(task_id)
        ):
            loop_id = _coerce_loop_id(row.get("loop_id"))
            if loop_id is None:
                continue
            if not (min_loop_id <= loop_id < current_loop_id):
                continue
            payload_raw = row.get("payload")
            if not isinstance(payload_raw, dict):
                continue
            payload = dict(payload_raw)
            if loop_id not in committed_loop_ids and not (
                loop_id in rejected_loop_ids
                and _is_prompt_safe_rejected_evidence(payload)
            ):
                continue
            evidence_type = str(payload.get("type", row.get("evidence_type", "")))
            if evidence_type != EvidenceType.TOOL_CALL.value:
                continue
            if not is_successful_evidence_payload(EvidenceType.TOOL_CALL, payload):
                continue
            evidence_id = str(row.get("evidence_id", ""))
            if not evidence_id:
                continue
            evidence_rows.append(
                (
                    loop_id,
                    index,
                    evidence_id,
                    summarize_tool_call(
                        payload,
                        loop_id,
                        max_chars=MAX_LINE_CHARS,
                    ),
                )
            )
        evidence_rows.sort(key=lambda item: (-item[0], item[1]))

        return _LoopHistorySlice(
            rejected_lines=rejected_lines,
            successful_evidence_ids=[row[2] for row in evidence_rows],
            successful_evidence_lines=[row[3] for row in evidence_rows],
            last_self_summary=last_summary,
        )

    def _should_emit_read_only_rejected_hint(
        self, task_id: str, current_loop_id: int
    ) -> bool:
        policy = self.repo.get_hunger_policy(task_id)
        if policy.completion_mode != CompletionMode.SPEND_BUDGET:
            return False
        traces = [
            trace
            for trace in self.repo.list_loop_traces(task_id)
            if not trace.committed and trace.loop_id < current_loop_id
        ]
        traces.sort(key=lambda trace: trace.loop_id, reverse=True)
        recent = traces[:2]
        if len(recent) < 2:
            return False
        evidence_rows = list(self.repo.list_successful_tool_call_evidence(task_id))
        for trace in recent:
            names: list[str] = []
            for row in evidence_rows:
                payload = row.get("payload")
                if (
                    _coerce_loop_id(row.get("loop_id")) == trace.loop_id
                    and isinstance(payload, dict)
                ):
                    names.append(str(payload.get("tool_name", "")))
            if not names or any(name != "read_file" for name in names):
                return False
        return True


def _coerce_loop_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_prompt_safe_rejected_evidence(payload: dict[str, object]) -> bool:
    """Allow read-only discoveries from rejected loops into retry context."""
    if str(payload.get("tool_name", "")) != "read_file":
        return False
    result = str(payload.get("result_summary", ""))
    return bool(result.strip())


def _clip_optional(value: str | None, max_chars: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if len(value) <= max_chars:
        return value, False
    return f"{value[: max_chars - 1]}…", True


def _clip_required(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"


def _clip_recent_summaries(
    *,
    prior_handoff_summary: str,
    last_summary: str | None,
) -> tuple[str, str | None]:
    clipped_prior = prior_handoff_summary[:MAX_HISTORY_CHARS]
    if last_summary is None:
        return clipped_prior, None
    remaining = max(0, MAX_HISTORY_CHARS - len(clipped_prior))
    return clipped_prior, last_summary[:remaining]


def _shape_workspace_files(
    raw_files: list[str],
    *,
    path_cap: int,
    line_cap: int,
    max_paths: int,
) -> list[str]:
    sorted_files = sorted(raw_files)
    selected = [_clip_required(path, path_cap) for path in sorted_files[:max_paths]]
    sentinel_count = max(0, len(sorted_files) - len(selected))

    def rendered(paths: list[str], count: int) -> str:
        shaped = [*paths]
        if count:
            shaped.append(f"… (+{count} more)")
        return "files in best/: " + ", ".join(shaped)

    while selected and len(rendered(selected, sentinel_count)) > line_cap:
        selected.pop()
        sentinel_count += 1

    out = [*selected]
    if sentinel_count:
        out.append(f"… (+{sentinel_count} more)")
    return out


def _assemble_history(
    *,
    loop_id: int,
    prior_handoff_summary: str,
    last_summary: str | None,
    best_summary: str | None,
    best_files: list[str],
    failure_lines: list[str],
    evidence_lines: list[str],
) -> str:
    return render_prior_loop_context_block(
        loop_id=loop_id,
        last_self_summary=last_summary,
        prior_handoff_summary=prior_handoff_summary,
        best_state_summary=best_summary,
        best_workspace_files=best_files,
        failure_patterns_to_avoid=failure_lines,
        relevant_evidence_summaries=evidence_lines,
    )


def _apply_history_cap(
    *,
    loop_id: int,
    prior_handoff_summary: str,
    last_summary: str | None,
    best_summary: str | None,
    best_files: list[str],
    evidence_ids: list[str],
    evidence_lines: list[str],
    failure_lines: list[str],
    best_summary_truncated: bool,
) -> tuple[str, str | None, list[str], list[str], list[str], TruncationInfo | None]:
    prior_handoff_summary, last_summary = _clip_recent_summaries(
        prior_handoff_summary=prior_handoff_summary,
        last_summary=last_summary,
    )
    assembled = _assemble_history(
        loop_id=loop_id,
        prior_handoff_summary=prior_handoff_summary,
        last_summary=last_summary,
        best_summary=best_summary,
        best_files=best_files,
        failure_lines=failure_lines,
        evidence_lines=evidence_lines,
    )
    if len(assembled) <= MAX_HISTORY_CHARS:
        return (
            prior_handoff_summary,
            last_summary,
            evidence_ids,
            evidence_lines,
            failure_lines,
            None,
        )

    chars_before = len(assembled)
    dropped_evidence = 0
    dropped_failures = 0
    while len(assembled) > MAX_HISTORY_CHARS and evidence_lines:
        evidence_lines.pop()
        evidence_ids.pop()
        dropped_evidence += 1
        assembled = _assemble_history(
            loop_id=loop_id,
            prior_handoff_summary=prior_handoff_summary,
            last_summary=last_summary,
            best_summary=best_summary,
            best_files=best_files,
            failure_lines=failure_lines,
            evidence_lines=evidence_lines,
        )
    while len(assembled) > MAX_HISTORY_CHARS and failure_lines:
        failure_lines.pop()
        dropped_failures += 1
        assembled = _assemble_history(
            loop_id=loop_id,
            prior_handoff_summary=prior_handoff_summary,
            last_summary=last_summary,
            best_summary=best_summary,
            best_files=best_files,
            failure_lines=failure_lines,
            evidence_lines=evidence_lines,
        )

    # If the non-evictable static block (last_summary + best_summary +
    # best_files + headers) alone exceeds MAX_HISTORY_CHARS, eviction
    # cannot help. Degrade gracefully: surface the over-cap state via
    # TruncationInfo.chars_after rather than crashing the loop with an
    # AssertionError. Downstream consumers (trace export, repair-state)
    # can detect the over-cap by comparing chars_after to the cap.
    chars_after = len(assembled)

    return (
        prior_handoff_summary,
        last_summary,
        evidence_ids,
        evidence_lines,
        failure_lines,
        TruncationInfo(
            chars_before=chars_before,
            chars_after=chars_after,
            dropped_failures=dropped_failures,
            dropped_evidence=dropped_evidence,
            truncated_best_summary=best_summary_truncated,
        ),
    )
