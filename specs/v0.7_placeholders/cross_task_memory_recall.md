# Cross-task Memory Recall

Placeholder for v0.7 P1 work to promote approved memories across task boundaries, retrieve relevant prior lessons into new mission context packs, and preserve evidence traceability for reused guidance.

## Delivered scope

This placeholder is fully delivered in v0.7. The implementation includes:

- `MemoryManager.auto_promote(task_id)`: predicate-gated auto-promotion after
  DONE stop reports, with upgraded candidate content containing check key,
  item title, check description, and prompt-safe evidence digests.
- `ContextPack.recalled_memories`: cross-task promoted-memory recall into
  worker context, capped at top 5 items and 1200 total characters.
- `ContextBuilder` recall logic and `ExecutionWorker` prior-mission insights
  rendering when `memory_recall_enabled=True` (default on, additive).

See `specs/v0.7_implementation/2026-07-07-loop-objective-evolution-design.md`
Section 5.2 for the full delivered memory promote and recall design.
