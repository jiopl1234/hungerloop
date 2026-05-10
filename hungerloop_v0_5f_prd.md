# HungerLoop v0.5f PRD — Cross-Loop Context Propagation

## 1. Goal

v0.5f makes loop execution stateful across attempts within the same
task. A worker should see what the previous loops tried, which checks
failed, which committed tool actions are already on record, and which
files exist in `best/`.

The implementation follows:

- `specs/v0.5f_loop_memory.spec.md`
- `specs/v0.5f_implementation/README.md`
- `specs/v0.5f_implementation/v0.5f.0_foundations.spec.md`
- `specs/v0.5f_implementation/v0.5f.1_context_builder.spec.md`
- `specs/v0.5f_implementation/v0.5f.2_wiring.spec.md`
- `specs/v0.5f_implementation/v0.5f.3_validation.spec.md`

## 2. Functional Scope

v0.5f adds:

- `ContextPack.last_self_summary`
- `ContextPack.relevant_evidence_summaries`
- `ContextPack.best_workspace_files`
- `ContextPack.truncation_info`
- `RepositoryProtocol.get_last_worker_result(task_id, agent_id, before_loop_id)`
- `WorkspaceReader` as a read-only workspace inventory Protocol
- `EventType.CONTEXT_TRUNCATED`

`ContextBuilder` owns all cross-loop history loading and prompt-safe
rendering. `ExecutionWorker` remains a pure consumer of `ContextPack`.
`LoopOrchestrator` owns the `context_truncated` audit event.

## 3. Non-Goals

- Cross-task memory recall.
- Multi-worker shared context in the same loop.
- Vector retrieval.
- LLM-based summarization.
- SQLite schema migrations.

## 4. Invariants

- Loop 1 with no prior history and empty `best/` preserves the
  post-`0568404` prompt shape.
- Only successful `tool_call` evidence from committed loops enters
  `relevant_evidence_*`.
- `model_call` evidence is represented by `last_self_summary`, not the
  evidence list.
- Paired `sandbox_run` rows are excluded to avoid double-counting
  `run_shell`.
- Context history is bounded by per-section caps and
  `MAX_HISTORY_CHARS`.
- `ContextBuilder.build_for_agent` is read-only.

## 5. Acceptance

Acceptance is defined by the sub-spec tasks LM0 through LM3:

- Foundations compile and serialize additively.
- ContextBuilder populates history deterministically.
- Orchestrator emits `context_truncated` before `worker_started`.
- ExecutionWorker renders the Prior-loop-context block only when
  history exists.
- Golden prompt and dummy cross-loop regression tests pass.
- `mypy --strict src/`, `ruff check src/ tests/`, and `pytest tests/`
  are green.
