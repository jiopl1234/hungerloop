# v0.5f Implementation TODOs

Per-task implementation index for the **Cross-Loop Context
Propagation** release and its immediate runtime follow-up. The
parent design spec is
[`../v0.5f_loop_memory.spec.md`](../v0.5f_loop_memory.spec.md);
the first four sub-specs below own the original v0.5f PR-sized
work units, and `v0.5f.4` extends that work into budgeted
iterative refinement.

## Why this exists

v0.5e.1 shipped the curation layer for skills + memory, but the
worker prompt is still loop-stateless. Real-LLM testing on
`flagship-1` showed `ls -la` running in Loop 1 and again in
Loop 3 because nothing tells the worker what previous loops
already did. v0.5f wires `ContextPack`'s three v0.4.1
forward-compat hooks (`relevant_claim_ids`,
`relevant_evidence_ids`, `failure_patterns_to_avoid`) plus four
new fields into the prompt — entirely inside `ContextBuilder`,
without touching the worker's I/O surface.

See parent spec §1.2 for the failure-mode trace and §10
(rev-3.1 changelog) for the prior reviews this design absorbed.

## Ordering

Strictly linear. Earlier sub-versions gate later ones:

1. **`v0.5f.0`** — additive `ContextPack` fields, one
   `RepositoryProtocol` read method, `WorkspaceReader` Protocol,
   one new `EventType`. No behavior change yet — every existing
   test stays green by construction.
2. **`v0.5f.1`** — `ContextBuilder` rewrite: loads loop history,
   populates the new fields, enforces the per-section + total
   caps. **Parent spec FR-5 / FR-6 / FR-7 land here.** This is
   the bulk of the v0.5f work.
3. **`v0.5f.2`** — wiring: orchestrator emits the truncation
   audit event, worker renders the new Prior-loop-context block,
   factory injects `WorkspaceManager` as `WorkspaceReader`.
4. **`v0.5f.3`** — validation: golden tests for prompt parity
   and history rendering, the deterministic dummy regression
   that anchors the "loop actually loops" claim, and the
   gated real-LLM smoke.
5. **`v0.5f.4`** — budgeted refinement worker loops: add an
   explicit spend-budget mode, deterministic refinement tiers,
   a normal `BUDGET_EXHAUSTED` terminal state, and CLI/runtime
   wiring so a user can intentionally spend loop budget on
   incremental improvement after tier-0 correctness is done.

Within a sub-version the task IDs follow the §5 implementation
TODO inside that spec.

## File map

| Spec file | Sub-release | LOC est. | Tasks | Depends on |
|---|---|---|---|---|
| [`v0.5f.0_foundations.spec.md`](./v0.5f.0_foundations.spec.md) | v0.5f.0 | ~120 + tests | LM0-01 → LM0-08 | v0.5e.1 + commit `0568404` |
| [`v0.5f.1_context_builder.spec.md`](./v0.5f.1_context_builder.spec.md) | v0.5f.1 | ~250 + tests | LM1-01 → LM1-10 | v0.5f.0 |
| [`v0.5f.2_wiring.spec.md`](./v0.5f.2_wiring.spec.md) | v0.5f.2 | ~80 + tests | LM2-01 → LM2-06 | v0.5f.1 |
| [`v0.5f.3_validation.spec.md`](./v0.5f.3_validation.spec.md) | v0.5f.3 | ~20 src + ~250 tests | LM3-01 → LM3-08 | v0.5f.2 |
| [`v0.5f.4_budgeted_refinement_worker.spec.md`](./v0.5f.4_budgeted_refinement_worker.spec.md) | v0.5f.4 | ~220 src + ~220 tests | LM4-01 → LM4-09 | v0.5f.3 |

LOC totals for the original v0.5f.0-v0.5f.3 track remain
~470 src + ~250 tests = **~720 LOC**. `v0.5f.4` is a follow-up
runtime spec with its own larger implementation budget.

## Hard gates

| Gate | Where | Why |
|---|---|---|
| **PRD `hungerloop_v0_5f_prd.md` lands before any code in v0.5f.0** | Parent §6 F1 + project convention since v0.4.1 | Spec → impl is the project's invariant; merging sub-spec code without the PRD section breaks reviewer mental model. |
| **`v0.5f.0` foundations ship green before `v0.5f.1` starts** | `LM1-FR-1` requires `WorkspaceReader` Protocol and the new `ContextPack` fields | Half-applied schema in `ContextBuilder` would leave Pydantic models with default-empty fields that never get populated, masking bugs. |
| **`v0.5f.1` builder must not call `repo.append_event`** | Parent NFR-7 | Read-path purity is the load-bearing reason `LoopOrchestrator` (not the builder) owns the truncation event. Pin via a unit test that asserts `repo.append_event` is never called from `build_for_agent`. |
| **Existing six dummy integration tests stay byte-identical** | Parent §5.8 / FR-11, asserted in `LM3-FR-5` | `DummyModelClient` ignores prompt content; if any of those tests change behavior, something else is wrong. |
| **Loop 1 prompt parity vs. post-`0568404`** | Parent §5.9 / FR-10, asserted in `LM3-FR-1` | The whole "additive only" framing of v0.5f rests on this test. |

## Reusable assets that v0.5f produces

- **`hungerloop.services.workspace_reader.WorkspaceReader`**
  Protocol: any future component (LearningWorker context, debug
  CLIs, repair-state extensions) that needs read-only file
  inventory should consume this Protocol rather than reach into
  `WorkspaceManager` internals.
- **`evidence_render.summarize_tool_call(payload, loop_id)`**
  helper (LM1-04): one-line evidence rendering used by both
  `_loop_history` and any future trace-export enrichment.
- **Closed-form proof of `MAX_HISTORY_CHARS` invariant** (parent
  NFR-5): future workers that grow context shapes can reuse the
  per-section + total-cap pattern.

## Task ID convention

`LM{sub}-{nn}` where `sub` is the sub-version digit (0/1/2/3/4)
and `nn` is the 2-digit sequence within the sub-version. Naming
matches the v0.5d/e convention (`B2-01`, `D0-01`, etc.) but uses
`LM` to avoid collision with parent spec's high-level F1–F13
implementation order.

## Status

| Sub-version | Status | Notes |
|---|---|---|
| v0.5f.0 | spec → ready | Pure additive; foundations only |
| v0.5f.1 | spec → ready | Largest chunk; do after v0.5f.0 lands |
| v0.5f.2 | spec → ready | Small wiring layer |
| v0.5f.3 | spec → ready | Tests only; no production source |
| v0.5f.4 | spec → ready | Budgeted iterative refinement after correctness |

Parent spec status: rev-3.1 (committed-loop semantic absorbed).
