"""Minimal v0.5a ExecutionWorker (PRD §8.2).

The worker has one job: ask :class:`ModelClient` for a structured action
plan and dispatch each action through :class:`ToolHarness`. It never
touches :class:`CandidateState` directly — :class:`Integrator` synthesises
that from the returned :class:`WorkerResult` (PRD §8.1).

Day 5 ships the minimum needed to pump the demo loop end-to-end with
:class:`DummyModelClient.with_actions(...)` and the four MVP tools.
Day 9 swaps in :class:`OpenAIModelClient`; nothing in this module needs
to change because retry kwargs are already threaded through from
:attr:`ContextPack.budget` (PRD §28.2 / M6).
"""
from __future__ import annotations

import json
from pathlib import Path

from hungerloop.models.context import ContextPack
from hungerloop.models.events import EventType
from hungerloop.models.worker import WorkerResult
from hungerloop.repository.protocol import RepositoryProtocol
from hungerloop.services.budget_guard import BudgetGuard
from hungerloop.services.model_client import ModelClient
from hungerloop.services.prior_loop_context import render_prior_loop_context_block
from hungerloop.services.tool_harness import ToolHarness, ToolResult
from hungerloop.services.tools import describe_tool_arg_schemas

# Inner self-repair: if the worker's first action batch contains any tool
# failure, re-invoke the model up to MAX_SELF_REPAIR_ITERATIONS extra times
# with the tool results stitched into the conversation. This makes the
# worker's inner loop deep enough to catch e.g. "pytest exited 1 — here is
# the stack trace" within a single hungerloop loop instead of waiting for
# the next loop's truncated evidence summary.
MAX_SELF_REPAIR_ITERATIONS = 5

# Cross-loop inner-loop replay (bug #7 fix): hold the LAST K (assistant,
# tool-followup) pairs of one loop's inner-loop in an instance cache,
# replay them inside the next loop's _messages() for the same
# (task_id, agent_id). Preserves the model's concrete reasoning chain
# (which lines it already tried changing, which tool outputs it actually
# observed) without sending the full conversation. Char-capped so the
# replay block can never blow past MAX_INNER_REPLAY_CHARS even on output-
# heavy iterations.
K_INNER_REPLAY = 3
MAX_INNER_REPLAY_CHARS = 16000
READ_ONLY_DIAGNOSTIC_THRESHOLD = 2


class ExecutionWorker:
    """LLM-driven worker that emits tool actions and dispatches them.

    The :class:`Worker` Protocol is structurally satisfied — the runtime
    only needs ``async run(*, context, workspace_root)`` and that's what
    this class exposes.
    """

    def __init__(
        self,
        model_client: ModelClient,
        tool_harness: ToolHarness,
        repo: RepositoryProtocol,
        budget_guard: BudgetGuard | None = None,
    ) -> None:
        self.model_client = model_client
        self.tool_harness = tool_harness
        self.repo = repo
        self.budget_guard = budget_guard
        # Cross-loop replay cache: keyed by (task_id, agent_id), holds
        # the last K (assistant, user-followup) pairs from the most
        # recent run(). Persisted only for the lifetime of this Worker
        # instance — `WorkerRuntime` keeps one Worker per agent_id for
        # the whole `hungerloop mission run` CLI invocation, which is
        # the span we care about.
        self._inner_replay: dict[tuple[str, str], list[dict[str, str]]] = {}

    def reset_inner_replay(self, task_id: str) -> None:
        """Drop cross-loop replay state for ``task_id``.

        Draft sampling calls this between samples: without it, draft j+1
        would replay draft j's stitched action pairs and the samples would
        not be independent.
        """
        for key in [k for k in self._inner_replay if k[0] == task_id]:
            del self._inner_replay[key]

    async def run(
        self, *, context: ContextPack, workspace_root: Path
    ) -> WorkerResult:
        """Run one worker iteration.

        Flow (PRD §8.2):

        1. Build messages from the mission + acceptance criteria.
        2. Ask the model for ``{"summary": str, "actions": [...]}``;
           retry kwargs are sourced from ``context.budget`` so the retry
           loop in :class:`OpenAIModelClient` honours the per-loop policy.
        3. Dispatch every action through :class:`ToolHarness`.
        4. Aggregate evidence and artifact ids into a :class:`WorkerResult`.

        :class:`ToolHarness` may raise :class:`ToolNotPermitted` or the
        :class:`BudgetGuard` may raise :class:`WorkerBudgetExceeded`;
        both are caught by :class:`WorkerRuntime` rather than this method
        so we don't double-handle the same exceptions.
        """
        replay_key = (context.task_id, context.agent_id)
        prior_replay = self._inner_replay.get(replay_key, [])
        messages = self._messages(context, prior_replay=prior_replay)
        seed_len = len(messages)
        evidence_ids: list[str] = []
        artifact_ids: list[str] = []
        summary = ""
        # A (tightened): "verified" now means "wrote/patched a file in
        # THIS loop AND then a run_shell succeeded" — not just "a
        # run_shell happened to succeed". Cross-loop replay surfaced the
        # bug: with the looser rule, a worker entering loop N (after
        # loop N-1 already committed a partial implementation) could
        # read the existing file, run a test that was already passing,
        # and call it verified — handing off without writing anything.
        # mini-sql loops 2/3/4 all exhibited exactly this: empty actions
        # accepted after pure-read iterations, 0 newly_passed, blocked.
        wrote_anything = False
        wrote_then_verified = False
        read_count = 0
        shell_count = 0
        write_attempt_count = 0
        patch_no_match_count = 0
        consecutive_nonwriting_iterations = 0
        blocked_nonwriting_action_count = 0
        needs_verification = bool(context.acceptance_criteria)

        for iteration in range(MAX_SELF_REPAIR_ITERATIONS + 1):
            if self.budget_guard is not None:
                self.budget_guard.assert_can_spend(context, addl_llm_calls=1)

            response = await self.model_client.complete_json(
                task_id=context.task_id,
                loop_id=context.loop_id,
                agent_id=context.agent_id,
                messages=messages,
                max_tokens=context.budget.max_tokens,
                max_retries=context.budget.max_model_retries,
                retry_base_delay_seconds=context.budget.retry_base_delay_seconds,
                retry_max_delay_seconds=context.budget.retry_max_delay_seconds,
            )

            if self.budget_guard is not None:
                used_tokens = (
                    response.usage.input_tokens + response.usage.output_tokens
                )
                self.budget_guard.record(
                    context.task_id,
                    context.loop_id,
                    context.agent_id,
                    tokens=used_tokens,
                    llm_calls=1,
                )
                self.budget_guard.assert_can_spend(context)

            if response.evidence_id is not None:
                evidence_ids.append(response.evidence_id)

            actions = self._extract_actions(response.json_data)
            action_results: list[tuple[str, ToolResult]] = []
            batch_has_write_action = any(
                self._stringify(action.get("tool_name", ""))
                in ("write_file", "patch_file")
                for action in actions
            )
            block_nonwriting_batch = (
                consecutive_nonwriting_iterations >= 2
                and not wrote_anything
                and bool(actions)
                and not batch_has_write_action
            )
            for action in actions:
                tool_name = self._stringify(action.get("tool_name", ""))
                args_raw = action.get("args", {})
                args = args_raw if isinstance(args_raw, dict) else {}

                if block_nonwriting_batch:
                    blocked_nonwriting_action_count += 1
                    result = ToolResult(
                        tool_name=tool_name,
                        success=False,
                        summary=(
                            "read-only action blocked after two consecutive "
                            "non-writing batches; use write_file or patch_file"
                        ),
                        error=(
                            "read_only_budget_exhausted: the next action batch "
                            "must edit the candidate before further inspection"
                        ),
                        error_type="read_only_budget_exhausted",
                    )
                else:
                    result = await self.tool_harness.execute(
                        context=context,
                        tool_name=tool_name,
                        args=args,
                        workspace_root=workspace_root,
                    )
                evidence_ids.extend(result.evidence_ids)
                artifact_ids.extend(result.artifact_ids)
                action_results.append((tool_name, result))

            patch_no_match_count += sum(
                tool_name == "patch_file"
                and not result.success
                and any(
                    marker in (result.error or result.summary)
                    for marker in (
                        "old_text not found",
                        "old_text matches",
                        "whitespace-normalized match is ambiguous",
                        "whitespace-normalized single-line patch requires",
                        "whitespace-normalized patch has an unsafe indentation",
                    )
                )
                for tool_name, result in action_results
            )

            new_summary = self._stringify(
                (response.json_data or {}).get("summary", "")
            ) if response.json_data else ""
            if new_summary:
                summary = (
                    new_summary
                    if not summary
                    else f"{summary}\n[repair iter {iteration + 1}] {new_summary}"
                )

            # Track the two halves separately so a write in iter K plus a
            # run_shell success in iter K+M (M>=0) counts as verified —
            # but a run_shell success without any prior write does NOT.
            this_iter_wrote = any(
                tn in ("write_file", "patch_file") and result.success
                for tn, result in action_results
            )
            read_count += sum(
                tn == "read_file" and result.success
                for tn, result in action_results
            )
            shell_count += sum(tn == "run_shell" for tn, _ in action_results)
            write_attempt_count += sum(
                tn in ("write_file", "patch_file")
                for tn, _ in action_results
            )
            this_iter_attempted_write = any(
                tn in ("write_file", "patch_file") for tn, _ in action_results
            )
            # Empty batches count as non-writing too — otherwise a model can
            # reset the mechanical block by interleaving empty responses
            # between read batches.
            if not this_iter_attempted_write:
                consecutive_nonwriting_iterations += 1
            else:
                consecutive_nonwriting_iterations = 0
            wrote_anything = wrote_anything or this_iter_wrote
            this_iter_shell_succeeded = any(
                tn == "run_shell" and r.success for tn, r in action_results
            )
            wrote_then_verified = wrote_then_verified or (
                wrote_anything and this_iter_shell_succeeded
            )
            verification_complete = wrote_then_verified or not needs_verification

            # Decide whether to do another self-repair iteration.
            if iteration >= MAX_SELF_REPAIR_ITERATIONS:
                break
            any_failed = any(not r.success for _, r in action_results)
            if action_results and not any_failed and verification_complete:
                break  # all tools good AND we've verified; commit to handoff
            if not action_results and verification_complete:
                break  # model handed off; either verification done or no criteria

            # Stitch results back so the next call has them in-context.
            # If the batch was empty AND we haven't seen a verification
            # pass yet, push the worker to add one rather than letting it
            # surrender silently.
            assistant_payload = json.dumps(response.json_data or {})
            if not action_results and not verification_complete:
                if not wrote_anything:
                    followup = (
                        "Your action batch was empty but you have NOT yet "
                        "written or patched any file in this loop. Reading "
                        "code and re-running tests that already pass does "
                        "not move any failing acceptance check from fail to "
                        "pass — only edits do. Emit a JSON action batch "
                        "that includes AT LEAST ONE write_file or "
                        "patch_file targeting the source you intend to fix, "
                        "followed by a run_shell that re-executes the "
                        "relevant verification command(s) from your "
                        "acceptance_criteria. Only return an empty actions "
                        "list AFTER you have written code AND a run_shell "
                        "verification has exited 0 in this same loop."
                    )
                else:
                    followup = (
                        "Your action batch was empty but although you HAVE "
                        "written/patched a file this loop, no run_shell has "
                        "yet exited 0 to verify the change took effect. "
                        "Emit a corrective JSON action batch that runs the "
                        "verification command(s) from your "
                        "acceptance_criteria via run_shell. Only return an "
                        "empty actions list after run_shell has exited 0."
                    )
            else:
                results_lines: list[str] = []
                for tn, r in action_results:
                    line = f"- {tn}: success={r.success}"
                    if r.error_type:
                        line += f" error_type={r.error_type}"
                    # Cap aligned with the patch_file tool's diagnostic
                    # budget (1500 chars) so the closest_matches /
                    # occurrences block emitted by failed patches is
                    # actually visible to the model on its repair turn.
                    if r.error:
                        line += "\n  error:\n    " + r.error[:1500].replace(
                            "\n", "\n    "
                        )
                    if r.output_excerpt:
                        line += "\n  output:\n    " + r.output_excerpt.replace(
                            "\n", "\n    "
                        )
                    elif r.summary and r.summary != r.error:
                        # Surface summary only when it carries info not
                        # already in `error` (which mirrors summary for
                        # most failure paths).
                        line += "\n  summary:\n    " + r.summary[:1500].replace(
                            "\n", "\n    "
                        )
                    results_lines.append(line)
                followup = (
                    "Tool results from your previous action batch:\n\n"
                    + "\n".join(results_lines)
                    + "\n\nIf any verification failed, emit a corrective "
                    "JSON action batch that fixes the underlying defect "
                    "AND re-runs the verification via run_shell so the "
                    "harness can see the new exit status. Only emit "
                    '{"summary": "...", "actions": []} after a run_shell '
                    "verification has exited 0 in this turn."
                )
                if not verification_complete and not wrote_anything:
                    followup += (
                        "\n\nMANDATORY EDIT: You have not written or patched "
                        "any source in this loop. A non-empty read_file or "
                        "run_shell batch does not count as progress. Your next "
                        "JSON action batch must include at least one write_file "
                        "or patch_file action that addresses a failing check, "
                        "followed by run_shell verification."
                    )
                if patch_no_match_count >= 2:
                    followup += (
                        "\n\nPATCH ESCALATION: patch_file has missed old_text at "
                        "least twice in this loop. Stop retrying guessed exact "
                        "patches. Use read_file with offset/limit to inspect the "
                        "current enclosing function, then use write_file with the "
                        "FULL current file content while replacing that complete "
                        "function. Re-run verification after the rewrite."
                    )
            remaining_calls = MAX_SELF_REPAIR_ITERATIONS - iteration
            budget_note = (
                f"CALL BUDGET: {remaining_calls} model call(s) remain in this "
                "loop after this response."
            )
            if consecutive_nonwriting_iterations >= 2 and not wrote_anything:
                followup = (
                    "STOP READING: consecutive action batches inspected files "
                    "without attempting an edit. "
                    f"{budget_note} Use the next response to patch/write the "
                    "best-supported fix and verify it; do not request another "
                    "code slice.\n\n"
                    + followup
                )
            else:
                followup += f"\n\n{budget_note}"
            if iteration == MAX_SELF_REPAIR_ITERATIONS - 1:
                followup += (
                    "\n\nFINAL MODEL CALL: your next response is the last "
                    "response available in this loop. It must contain a "
                    "write_file/patch_file edit and run_shell verification. "
                    "A read-only response will be discarded with no progress."
                )
            messages = [
                *messages,
                {"role": "assistant", "content": assistant_payload},
                {"role": "user", "content": followup},
            ]

        if (
            (read_count > 0 or shell_count > 0)
            and write_attempt_count == 0
            and needs_verification
        ):
            streak = self._read_only_streak_before_loop(
                task_id=context.task_id,
                agent_id=context.agent_id,
                loop_id=context.loop_id,
            ) + 1
            self.repo.append_event(
                EventType.WORKER_READ_ONLY_STREAK,
                {
                    "agent_id": context.agent_id,
                    "streak": streak,
                    "read_count": read_count,
                    "shell_count": shell_count,
                    "write_count": write_attempt_count,
                    "blocked_nonwriting_action_count": (
                        blocked_nonwriting_action_count
                    ),
                    "threshold_reached": (
                        streak >= READ_ONLY_DIAGNOSTIC_THRESHOLD
                    ),
                    "context_truncated": context.truncation_info is not None,
                    "failing_check_keys": list(context.failing_check_keys),
                    "target_hunger_item_ids": list(
                        context.target_hunger_item_ids
                    ),
                },
                task_id=context.task_id,
                loop_id=context.loop_id,
            )

        replay_entries = list(messages[seed_len:])
        final_reads = [
            result
            for tool_name, result in action_results
            if tool_name == "read_file" and result.success
        ]
        if final_reads and not this_iter_wrote:
            read_lines = []
            for result in final_reads:
                detail = result.output_excerpt or result.summary
                read_lines.append(
                    "- read_file: success=True\n  output:\n    "
                    + detail.replace("\n", "\n    ")
                )
            replay_entries.extend(
                [
                    {
                        "role": "assistant",
                        "content": json.dumps(response.json_data or {}),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Read results from the final action batch of this "
                            "loop:\n\n" + "\n".join(read_lines)
                        ),
                    },
                ]
            )

        # Persist the last K stitched pairs from this loop's inner-loop
        # so the NEXT run() for the same (task, agent) can replay them.
        # Normal stitched pairs come from ``messages``. A final successful
        # read batch is added explicitly above because the loop can break
        # before the normal follow-up stitch runs.
        self._inner_replay[replay_key] = _build_replay_block(replay_entries)

        return WorkerResult(
            agent_id=context.agent_id,
            task_id=context.task_id,
            loop_id=context.loop_id,
            summary=summary,
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
        )

    def _read_only_streak_before_loop(
        self,
        *,
        task_id: str,
        agent_id: str,
        loop_id: int,
    ) -> int:
        events = self.repo.list_events(
            task_id,
            until_loop=max(0, loop_id - 1),
            event_types=[EventType.WORKER_READ_ONLY_STREAK.value],
        )
        for event in reversed(events):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("agent_id") != agent_id:
                continue
            if event.get("loop_id") != loop_id - 1:
                return 0
            streak = payload.get("streak")
            return streak if isinstance(streak, int) else 0
        return 0

    @staticmethod
    def _messages(
        context: ContextPack,
        *,
        prior_replay: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        """Build a minimal chat-completions message list (PRD §8.2 step 1).

        When ``prior_replay`` is supplied, it is appended between the
        seed user message and the next live assistant turn so the model
        can see the actual JSON it emitted last loop and the tool
        outputs it observed. A boundary marker is appended to the last
        user message of the replay to flag the new loop start.
        """
        keys = context.acceptance_check_keys
        if keys and len(keys) == len(context.acceptance_criteria):
            acceptance_lines = "\n".join(
                f"- [{key}] {item}"
                for key, item in zip(keys, context.acceptance_criteria, strict=False)
            ) or "- (none provided)"
        else:
            acceptance_lines = "\n".join(
                f"- {item}" for item in context.acceptance_criteria
            ) or "- (none provided)"
        progress_block = ExecutionWorker._acceptance_progress_block(context)
        tool_schema_lines = describe_tool_arg_schemas(context.allowed_tools)
        system = (
            "You are an execution worker that emits structured JSON actions. "
            "Respond only with a JSON object containing 'summary' (string) "
            "and 'actions' (list of {tool_name, args}). Do not wrap the JSON "
            "in Markdown.\n\n"
            "SELF-VERIFY BEFORE HANDOFF — STRICT RULE:\n"
            "1. After every write/edit, include a run_shell action that "
            "   runs the verification command(s) from acceptance_criteria.\n"
            "2. The harness will give you multiple follow-up turns whenever "
            "   any tool fails OR whenever you emit an empty action list "
            "   before a verification has succeeded.\n"
            "3. If a verification fails: rewrite the file(s) with corrected "
            "   logic and re-run the verification IN THE SAME TURN. Do not "
            "   hand off with 'actions: []' until run_shell exits 0.\n"
            "4. 'I don't know how to fix this' is never a valid reason to "
            "   hand off — keep iterating until either the verification "
            "   passes or the harness exhausts your repair budget.\n"
            "5. Reading files and running tests that already pass is NOT "
            "   progress. The harness will reject your empty-actions "
            "   handoff unless THIS loop contains at least one "
            "   write_file/patch_file action AND a subsequent run_shell "
            "   that exited 0. If the existing implementation has bugs, "
            "   you must patch them — not just inspect them."
        )
        prior_context = ExecutionWorker._prior_loop_context(context)
        recall_block = ExecutionWorker._recalled_memories_block(context)
        user = (
            f"Mission:\n{context.mission}\n\n"
            f"Acceptance criteria:\n{acceptance_lines}\n\n"
            f"{progress_block}"
            f"Allowed tools and args schema:\n{tool_schema_lines}\n\n"
            f"{prior_context}"
            f"{recall_block}"
            "Required JSON shape example:\n"
            '{"summary":"created hello.txt","actions":[{"tool_name":'
            '"write_file","args":{"path":"hello.txt","content":"hello"}}]}\n\n'
            "Use exactly the listed args shape for each tool. For run_shell, "
            "use an argv array and never a command string."
        )
        if prior_replay:
            pair_count = len(prior_replay) // 2
            user = user + (
                f"\n\nReplay of the last {pair_count} action/result pair(s) "
                f"from loop {max(0, context.loop_id - 1)} follows. Treat the "
                "assistant messages below as JSON YOU emitted in that loop, "
                "and the user messages as the tool outputs you saw. Skip "
                "already-tried fixes; build on what worked. After the "
                "replay ends, you will begin a fresh action batch for the "
                "current loop."
            )
            msgs: list[dict[str, str]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                *prior_replay,
            ]
            boundary_note = (
                f"\n\n--- End replay. Begin loop {context.loop_id} now. "
                "Issue your next JSON action batch. ---"
            )
            if msgs[-1]["role"] == "user":
                msgs[-1] = {
                    "role": "user",
                    "content": msgs[-1]["content"] + boundary_note,
                }
            else:
                msgs.append(
                    {"role": "user", "content": boundary_note.lstrip()}
                )
            return msgs
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _acceptance_progress_block(context: ContextPack) -> str:
        """Render the pass/fail status of acceptance check_keys at start of loop.

        Empty string when there's nothing to report (no keys, or every
        criterion is still pending without best/ history). When best/
        already passes some checks, the block tells the worker exactly
        which criteria are already green (don't regress) and which keys
        to focus on. Generic: only uses check_key strings the operator
        already put in the mission ledger — no descriptive text.
        """
        passed = context.passed_check_keys
        failing = context.failing_check_keys
        if not passed and not failing:
            return ""
        total = len(passed) + len(failing)
        lines = [
            "Acceptance check status in best/ workspace at start of this loop:",
            f"- {len(passed)} of {total} ALREADY PASSING — do NOT regress.",
        ]
        if failing:
            # Cap failing keys list to keep prompt bounded. The model can
            # cross-reference [<key>] tags in the acceptance criteria
            # list above. Show first MAX_FAILING_KEYS, then "+M more".
            MAX_FAILING_KEYS = 40
            shown = failing[:MAX_FAILING_KEYS]
            more = max(0, len(failing) - len(shown))
            keys_csv = ", ".join(shown)
            tail = f", +{more} more" if more else ""
            lines.append(
                f"- {len(failing)} still FAILING — focus inner-loop here. "
                f"Keys (cross-reference [<key>] in the list above): "
                f"{keys_csv}{tail}"
            )
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _prior_loop_context(context: ContextPack) -> str:
        return render_prior_loop_context_block(
            loop_id=context.loop_id,
            last_self_summary=context.last_self_summary,
            prior_handoff_summary=context.prior_handoff_summary,
            best_state_summary=context.best_state_summary,
            best_workspace_files=context.best_workspace_files,
            failure_patterns_to_avoid=context.failure_patterns_to_avoid,
            relevant_evidence_summaries=context.relevant_evidence_summaries,
        )

    @staticmethod
    def _recalled_memories_block(context: ContextPack) -> str:
        """Render a labeled prior-mission insights section.

        When ``context.recalled_memories`` is non-empty, render a clearly
        labeled section containing the recalled strings as bullet points.
        When empty, return an empty string so existing prompt sections
        retain their previous content and ordering (VAL-MEM-013).
        """
        if not context.recalled_memories:
            return ""
        lines = [
            "Prior-mission insights (reusable cross-task memory):",
        ]
        for insight in context.recalled_memories:
            lines.append(f"  - {insight}")
        lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _extract_actions(
        json_data: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        """Pull the ``actions`` list off the model response, defensively."""
        if not json_data:
            return []
        raw = json_data.get("actions")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _stringify(value: object) -> str:
        return value if isinstance(value, str) else str(value)


def _total_chars(msgs: list[dict[str, str]]) -> int:
    return sum(len(m.get("content", "")) for m in msgs)


def _build_replay_block(
    new_entries: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return the trimmed (assistant, user) replay block to carry over.

    Strategy:
      1. Take the most recent ``K_INNER_REPLAY`` pairs (i.e. 2K messages).
      2. Drop the OLDEST whole pair until the total content size is
         under ``MAX_INNER_REPLAY_CHARS`` OR only one pair remains.
      3. If one pair still exceeds the cap, head+tail clip the user
         followup's content while keeping the assistant payload intact —
         the assistant's JSON is the part the model needs most to
         remember what it tried.
    """
    if not new_entries:
        return []
    tail = list(new_entries[-(K_INNER_REPLAY * 2):])
    while _total_chars(tail) > MAX_INNER_REPLAY_CHARS and len(tail) > 2:
        tail = tail[2:]
    if _total_chars(tail) > MAX_INNER_REPLAY_CHARS and len(tail) == 2:
        asst, user = tail
        asst_len = len(asst.get("content", ""))
        budget = max(500, MAX_INNER_REPLAY_CHARS - asst_len - 80)
        head = int(budget * 0.6)
        clip_tail = max(200, budget - head)
        original = user.get("content", "")
        clipped = (
            original[:head]
            + f"\n…[{max(0, len(original) - head - clip_tail)} chars clipped]…\n"
            + original[-clip_tail:]
        )
        tail = [asst, {"role": "user", "content": clipped}]
    return tail
