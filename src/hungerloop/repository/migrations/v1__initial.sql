-- HungerLoop v0.5a SQLite schema (PRD §17.2)
-- Design-only; SQLiteRepository implementation is Day 3+.
-- This file documents the target schema; InMemoryRepository remains the
-- v0.5a runtime for tests and CLI.

PRAGMA user_version = 1;
PRAGMA foreign_keys = ON;

-- =====================================================================
-- Section 1: Tasks
-- =====================================================================
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,  -- 'pending', 'running', 'stopped'
  last_stop_reason TEXT,  -- StopReason enum value
  created_at TEXT NOT NULL,  -- ISO-8601 UTC
  updated_at TEXT NOT NULL
);

-- =====================================================================
-- Section 2: Hunger (policy / clock / ledger / items / snapshots)
-- =====================================================================
CREATE TABLE hunger_policies (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  payload_json TEXT NOT NULL  -- HungerPolicy serialized
);

CREATE TABLE hunger_clocks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  payload_json TEXT NOT NULL  -- HungerClockState serialized
);

CREATE TABLE hunger_ledgers (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  payload_json TEXT NOT NULL  -- HungerLedger serialized (items are also in hunger_items)
);

CREATE TABLE hunger_items (
  item_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  status TEXT NOT NULL,  -- HungerItemStatus enum
  gap_score REAL NOT NULL,
  priority REAL NOT NULL,
  consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
  last_progress_loop_id INTEGER,
  payload_json TEXT NOT NULL  -- HungerItem serialized
);

CREATE INDEX idx_hunger_items_task ON hunger_items(task_id);
CREATE INDEX idx_hunger_items_status ON hunger_items(task_id, status);

CREATE TABLE hunger_snapshots (
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  phase TEXT NOT NULL,  -- LoopPhase enum
  payload_json TEXT NOT NULL,  -- HungerSnapshot serialized
  PRIMARY KEY (task_id, loop_id)
);

-- =====================================================================
-- Section 3: Workspace state (best / candidates)
-- =====================================================================
CREATE TABLE best_states (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  state_id TEXT NOT NULL,
  payload_json TEXT NOT NULL  -- BestState serialized
);

CREATE TABLE candidates (
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  status TEXT NOT NULL,  -- 'pending', 'committed', 'rejected'
  payload_json TEXT NOT NULL  -- CandidateState serialized
);

CREATE INDEX idx_candidates_task_loop ON candidates(task_id, loop_id);

-- =====================================================================
-- Section 4: Validation
-- =====================================================================
CREATE TABLE validation_reports (
  validation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  verdict TEXT NOT NULL,  -- ValidationVerdict enum
  payload_json TEXT NOT NULL  -- ValidationReport serialized
);

CREATE INDEX idx_validation_task_loop ON validation_reports(task_id, loop_id);

CREATE TABLE accepted_checks (
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  check_key TEXT NOT NULL,
  hunger_item_id TEXT NOT NULL,
  check_index INTEGER NOT NULL,
  accepted_at_loop INTEGER NOT NULL,
  validation_id TEXT NOT NULL,
  evidence_id TEXT,
  PRIMARY KEY (task_id, check_key)
);

CREATE INDEX idx_accepted_checks_item ON accepted_checks(hunger_item_id);

-- =====================================================================
-- Section 5: Evidence
-- =====================================================================
CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER,
  evidence_type TEXT NOT NULL,  -- EvidenceType enum value
  payload_json TEXT NOT NULL
);

CREATE INDEX idx_evidence_task_loop ON evidence(task_id, loop_id);
CREATE INDEX idx_evidence_type ON evidence(task_id, evidence_type);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  artifact_type TEXT NOT NULL,
  path TEXT,
  summary TEXT,
  payload_json TEXT NOT NULL  -- Artifact serialized
);

CREATE INDEX idx_artifacts_task_loop ON artifacts(task_id, loop_id);

-- =====================================================================
-- Section 6: Worker / Planning
-- =====================================================================
CREATE TABLE agent_specs (
  agent_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL  -- AgentSpec serialized
);

CREATE TABLE worker_results (
  result_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  payload_json TEXT NOT NULL  -- WorkerResult serialized
);

CREATE INDEX idx_worker_results_task_loop ON worker_results(task_id, loop_id);

CREATE TABLE loop_plans (
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  payload_json TEXT NOT NULL,  -- LoopPlan serialized
  PRIMARY KEY (task_id, loop_id)
);

-- =====================================================================
-- Section 7: Trace / Stop
-- =====================================================================
CREATE TABLE loop_traces (
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  phase TEXT NOT NULL,
  committed INTEGER NOT NULL,  -- 0 or 1
  payload_json TEXT NOT NULL,  -- LoopTrace serialized
  PRIMARY KEY (task_id, loop_id)
);

CREATE TABLE stop_reports (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  stop_reason TEXT NOT NULL,  -- StopReason enum
  created_at TEXT NOT NULL,  -- ISO-8601 UTC
  payload_json TEXT NOT NULL  -- StopReport serialized
);

CREATE INDEX idx_stop_reports_task ON stop_reports(task_id, created_at DESC);

-- =====================================================================
-- Section 8: Memory / Skill
-- =====================================================================
CREATE TABLE memory_candidates (
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  status TEXT NOT NULL,  -- 'candidate', 'approved', 'rejected'
  memory_type TEXT NOT NULL,  -- 'fact', 'procedure', 'preference', 'pitfall'
  payload_json TEXT NOT NULL  -- MemoryCandidate serialized
);

CREATE INDEX idx_memory_task_status ON memory_candidates(task_id, status);

CREATE TABLE skill_cards (
  skill_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  name TEXT NOT NULL,
  payload_json TEXT NOT NULL  -- SkillCard serialized
);

CREATE INDEX idx_skill_cards_task ON skill_cards(task_id);

-- =====================================================================
-- Section 9: Misc
-- =====================================================================
CREATE TABLE no_progress_streak (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  granted_at TEXT NOT NULL  -- ISO-8601 UTC
);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(task_id),
  loop_id INTEGER,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL  -- ISO-8601 UTC
);

CREATE INDEX idx_events_task ON events(task_id, created_at);
CREATE INDEX idx_events_type ON events(event_type);
