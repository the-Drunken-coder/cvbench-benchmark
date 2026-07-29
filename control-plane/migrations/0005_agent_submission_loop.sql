CREATE TABLE submission_artifacts (
  id TEXT PRIMARY KEY,
  submitter_key_sha256 TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  multipart_upload_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('uploading', 'ready', 'aborted')),
  archive_sha256 TEXT NOT NULL,
  archive_size INTEGER NOT NULL,
  image_id TEXT NOT NULL,
  compression TEXT NOT NULL CHECK (compression = 'gzip'),
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);

CREATE INDEX submission_artifacts_owner_idx
  ON submission_artifacts (submitter_key_sha256, created_at);

CREATE TABLE submission_artifact_parts (
  artifact_id TEXT NOT NULL REFERENCES submission_artifacts(id) ON DELETE CASCADE,
  part_number INTEGER NOT NULL,
  etag TEXT NOT NULL,
  byte_count INTEGER NOT NULL,
  PRIMARY KEY (artifact_id, part_number)
);

ALTER TABLE submissions ADD COLUMN transport_type TEXT NOT NULL DEFAULT 'registry'
  CHECK (transport_type IN ('registry', 'uploaded_oci'));
ALTER TABLE submissions ADD COLUMN artifact_id TEXT;
ALTER TABLE submissions ADD COLUMN artifact_object_key TEXT;
ALTER TABLE submissions ADD COLUMN artifact_archive_sha256 TEXT;
ALTER TABLE submissions ADD COLUMN artifact_archive_size INTEGER;
ALTER TABLE submissions ADD COLUMN artifact_image_id TEXT;
ALTER TABLE submissions ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE submissions ADD COLUMN progress_message TEXT;
ALTER TABLE submissions ADD COLUMN progress_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE submissions ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 16;

UPDATE submissions
SET
  progress_stage = CASE status
    WHEN 'running' THEN 'runner_started'
    WHEN 'succeeded' THEN 'completed'
    WHEN 'failed' THEN 'failed'
    ELSE 'queued'
  END,
  progress_message = CASE status
    WHEN 'running' THEN 'Trusted runner started.'
    WHEN 'succeeded' THEN 'Benchmark completed.'
    WHEN 'failed' THEN 'Benchmark failed.'
    ELSE 'Waiting for a trusted runner.'
  END,
  progress_completed = CASE WHEN status = 'succeeded' THEN 16 ELSE 0 END;

CREATE INDEX submissions_artifact_idx ON submissions (artifact_id);
