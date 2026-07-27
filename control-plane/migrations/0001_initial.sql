CREATE TABLE submissions (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  image TEXT NOT NULL,
  argv_json TEXT NOT NULL,
  name TEXT NOT NULL,
  model_version TEXT NOT NULL,
  contact TEXT,
  notes TEXT,
  idempotency_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  submitter_key_sha256 TEXT NOT NULL,
  lease_token_sha256 TEXT,
  lease_expires_at INTEGER,
  attempt INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER,
  UNIQUE (submitter_key_sha256, idempotency_key)
);

CREATE INDEX submissions_queue_idx ON submissions (status, created_at);
CREATE INDEX submissions_rate_idx ON submissions (submitter_key_sha256, created_at);
