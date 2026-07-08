-- HungerLoop v0.7 refactor transactions schema (ADR-010).

CREATE TABLE IF NOT EXISTS refactor_transactions (
  transaction_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  status TEXT NOT NULL,
  opening_loop INTEGER NOT NULL,
  deadline_loop INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);

-- Index for task + status lookups (e.g. get_open_refactor_transaction).
CREATE INDEX IF NOT EXISTS idx_refactor_txns_task_status
  ON refactor_transactions(task_id, status);

-- Partial unique index: at most one open transaction per task.
-- Multiple closed_success or rolled_back rows are allowed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_refactor_txns_open_uniq
  ON refactor_transactions(task_id)
  WHERE status = 'open';

PRAGMA user_version = 7;
