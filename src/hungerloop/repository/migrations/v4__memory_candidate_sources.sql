-- HungerLoop v0.5c memory source links.
-- Allows promotion predicates to reason about the validated source state
-- instead of comparing a MemoryCandidate id to a BestState id.

ALTER TABLE memory_candidates ADD COLUMN source_candidate_state_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN source_validation_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN source_best_state_id TEXT;

CREATE INDEX idx_memory_source_best ON memory_candidates(task_id, source_best_state_id);

PRAGMA user_version = 4;
