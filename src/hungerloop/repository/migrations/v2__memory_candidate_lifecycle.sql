-- HungerLoop v0.5c.0 schema bump (PRD §19.1).
-- Adds the forward-compat lifecycle fields to memory_candidates so v0.6
-- promotion can be a pure read-and-flip without a breaking migration.

ALTER TABLE memory_candidates ADD COLUMN state TEXT NOT NULL DEFAULT 'proposed';
ALTER TABLE memory_candidates ADD COLUMN decision_loop_id INTEGER;
ALTER TABLE memory_candidates ADD COLUMN decided_by TEXT;
ALTER TABLE memory_candidates ADD COLUMN decision_rationale TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_candidates ADD COLUMN replaces_candidate_id TEXT;
ALTER TABLE memory_candidates ADD COLUMN expires_at TEXT;  -- ISO8601 UTC, nullable

CREATE INDEX idx_memory_state ON memory_candidates(task_id, state);

PRAGMA user_version = 2;
