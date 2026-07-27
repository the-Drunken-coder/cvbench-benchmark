ALTER TABLE submissions ADD COLUMN result_sha256 TEXT;

ALTER TABLE operator_notes ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'legacy-operator';

CREATE INDEX submissions_result_sha_idx ON submissions (result_sha256);
