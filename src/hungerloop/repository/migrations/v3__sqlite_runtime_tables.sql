-- HungerLoop v0.5b runtime persistence schema fixes.
-- Adds explicit usage/task-lock tables and rebuilds evidence with the
-- EvidenceType CHECK for databases created before this constraint existed.

CREATE TABLE IF NOT EXISTS usage_snapshots (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0.0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_locks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
  owner TEXT NOT NULL,
  locked_at TEXT NOT NULL
);

PRAGMA foreign_keys = OFF;

CREATE TABLE hunger_items_runtime (
  item_id TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  status TEXT NOT NULL,
  gap_score REAL NOT NULL,
  priority REAL NOT NULL,
  consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
  last_progress_loop_id INTEGER,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, item_id)
);

INSERT INTO hunger_items_runtime (
  item_id,
  task_id,
  status,
  gap_score,
  priority,
  consecutive_failure_count,
  last_progress_loop_id,
  payload_json
)
SELECT
  item_id,
  task_id,
  status,
  gap_score,
  priority,
  consecutive_failure_count,
  last_progress_loop_id,
  payload_json
FROM hunger_items;

DROP TABLE hunger_items;
ALTER TABLE hunger_items_runtime RENAME TO hunger_items;

CREATE TABLE evidence_checked (
  evidence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER,
  evidence_type TEXT NOT NULL CHECK (
    evidence_type IN (
      'sandbox_run',
      'model_call',
      'model_error',
      'validation_check',
      'tool_call',
      'human_input',
      'discovered_fact_compiled'
    )
  ),
  payload_json TEXT NOT NULL
);

INSERT INTO evidence_checked (
  evidence_id,
  task_id,
  loop_id,
  evidence_type,
  payload_json
)
SELECT
  evidence_id,
  task_id,
  loop_id,
  evidence_type,
  payload_json
FROM evidence;

DROP TABLE evidence;
ALTER TABLE evidence_checked RENAME TO evidence;

PRAGMA foreign_keys = ON;

CREATE INDEX idx_evidence_task_loop ON evidence(task_id, loop_id);
CREATE INDEX idx_evidence_type ON evidence(task_id, evidence_type);
CREATE INDEX idx_hunger_items_task ON hunger_items(task_id);
CREATE INDEX idx_hunger_items_status ON hunger_items(task_id, status);

PRAGMA user_version = 3;
