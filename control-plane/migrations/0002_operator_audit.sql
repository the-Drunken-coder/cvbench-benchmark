CREATE TABLE operator_notes (
  id TEXT PRIMARY KEY,
  submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  verdict TEXT NOT NULL CHECK (verdict IN ('unreviewed', 'needs_review', 'adjudicated', 'accepted', 'rejected')),
  note TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  operator_key_sha256 TEXT NOT NULL
);

CREATE INDEX operator_notes_submission_idx ON operator_notes (submission_id, created_at);
