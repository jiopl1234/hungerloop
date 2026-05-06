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

-- v0.5e.1 skill lifecycle tables (PRD §18 / FR-15).
-- Path A: v0.5e.0 has not shipped externally, so we append in place
-- rather than creating a v5_addendum file. The PRAGMA below stays at
-- 5 — both increments share the same DB version.

CREATE TABLE IF NOT EXISTS skill_card_candidates (
  skill_candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  source_best_state_id TEXT NOT NULL,
  source_stop_report_id TEXT,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_card_candidates_task
  ON skill_card_candidates(task_id);
CREATE INDEX IF NOT EXISTS idx_skill_card_candidates_state
  ON skill_card_candidates(state);

CREATE TABLE IF NOT EXISTS active_skill_cards (
  skill_id TEXT PRIMARY KEY,
  source_candidate_id TEXT NOT NULL,
  name TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  activated_at TEXT NOT NULL,
  activated_by TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_active_skill_cards_state
  ON active_skill_cards(state);

PRAGMA user_version = 5;
