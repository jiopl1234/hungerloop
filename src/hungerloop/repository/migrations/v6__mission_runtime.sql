-- HungerLoop v0.6 mission runtime schema (PRD §12.4).

CREATE TABLE IF NOT EXISTS missions (
  mission_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_phases (
  phase_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_features (
  feature_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  phase_id TEXT NOT NULL REFERENCES mission_phases(phase_id),
  hunger_item_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_handoffs (
  handoff_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  loop_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_assertions (
  assertion_id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(mission_id),
  phase_id TEXT NOT NULL REFERENCES mission_phases(phase_id),
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phases_mission
  ON mission_phases(mission_id);
CREATE INDEX IF NOT EXISTS idx_features_phase
  ON mission_features(phase_id);
CREATE INDEX IF NOT EXISTS idx_assertions_phase
  ON validation_assertions(phase_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_loop
  ON worker_handoffs(task_id, loop_id);

PRAGMA user_version = 6;
