-- HungerLoop v0.5e.0 memory lifecycle extensions (FR-20, FR-21).
--
-- Adds the predicate columns and reviewer audit columns the
-- v0.5e.0 PromoteValidator and ApprovalEngine need; introduces
-- the promoted_memories table that ApprovalEngine writes after a
-- candidate is approved.
--
-- expires_at is intentionally NOT redeclared — it shipped in v2.
-- source_candidate_state_id / source_validation_id /
-- source_best_state_id shipped in v4.

ALTER TABLE memory_candidates ADD COLUMN accepted_check_keys_json
    TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_candidates ADD COLUMN action_verified
    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN reusable
    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN non_volatile
    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN traceable
    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_candidates ADD COLUMN reviewer TEXT;
ALTER TABLE memory_candidates ADD COLUMN reviewed_at TEXT;
ALTER TABLE memory_candidates ADD COLUMN rejection_reason TEXT;

CREATE TABLE IF NOT EXISTS promoted_memories (
  memory_id TEXT PRIMARY KEY,
  source_candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id),
  task_id TEXT NOT NULL,
  content TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  layer TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  accepted_check_keys_json TEXT NOT NULL,
  reuse_scenarios_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_promoted_memories_task
  ON promoted_memories(task_id);
CREATE INDEX IF NOT EXISTS idx_promoted_memories_source
  ON promoted_memories(source_candidate_id);

PRAGMA user_version = 5;
