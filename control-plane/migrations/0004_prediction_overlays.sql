ALTER TABLE submissions ADD COLUMN prediction_overlays_required INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submissions ADD COLUMN prediction_overlay_attempt INTEGER;
ALTER TABLE submissions ADD COLUMN prediction_overlay_root_sha256 TEXT;

CREATE TABLE prediction_overlays (
  submission_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  scenario_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  prediction_count INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (submission_id, attempt, scenario_id),
  FOREIGN KEY (submission_id) REFERENCES submissions(id)
);

CREATE TABLE prediction_overlay_sets (
  submission_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  root_sha256 TEXT NOT NULL,
  sealed_at INTEGER NOT NULL,
  PRIMARY KEY (submission_id, attempt),
  FOREIGN KEY (submission_id) REFERENCES submissions(id)
);
