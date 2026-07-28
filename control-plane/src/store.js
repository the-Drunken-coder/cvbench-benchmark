export class D1Store {
  constructor(db) {
    this.db = db;
  }

  async health() {
    await this.db.prepare("SELECT COUNT(*) AS count FROM submissions").first();
  }

  async createArtifact(row, maxPerHour) {
    const changed = await this.db.prepare(`INSERT INTO submission_artifacts (
      id, submitter_key_sha256, object_key, multipart_upload_id, status,
      archive_sha256, archive_size, image_id, compression, created_at
    ) SELECT ?, ?, ?, ?, 'uploading', ?, ?, ?, 'gzip', ?
    WHERE (
      SELECT COUNT(*) FROM submission_artifacts
      WHERE submitter_key_sha256 = ? AND created_at >= ?
    ) < ?`)
      .bind(
        row.id,
        row.submitterKeyHash,
        row.objectKey,
        row.multipartUploadId,
        row.archiveSha256,
        row.archiveSize,
        row.imageId,
        row.now,
        row.submitterKeyHash,
        row.now - 3600,
        maxPerHour,
      )
      .run();
    return Number(changed.meta?.changes || 0) === 1 ? this.getArtifact(row.id) : null;
  }

  async getArtifact(id) {
    const row = await this.db.prepare("SELECT * FROM submission_artifacts WHERE id = ?").bind(id).first();
    return row ? deserializeArtifact(row) : null;
  }

  async recordArtifactPart({ id, partNumber, etag, byteCount }) {
    await this.db.prepare(`INSERT INTO submission_artifact_parts (artifact_id, part_number, etag, byte_count)
      VALUES (?, ?, ?, ?)
      ON CONFLICT (artifact_id, part_number) DO UPDATE SET
        etag = excluded.etag, byte_count = excluded.byte_count`)
      .bind(id, partNumber, etag, byteCount)
      .run();
    return this.listArtifactParts(id);
  }

  async listArtifactParts(id) {
    const result = await this.db.prepare(`SELECT part_number, etag, byte_count
      FROM submission_artifact_parts WHERE artifact_id = ? ORDER BY part_number`)
      .bind(id)
      .all();
    return (result.results || []).map((row) => ({
      partNumber: row.part_number,
      etag: row.etag,
      byteCount: row.byte_count,
    }));
  }

  async completeArtifact({ id, now }) {
    const changed = await this.db.prepare(`UPDATE submission_artifacts
      SET status = 'ready', completed_at = ?
      WHERE id = ? AND status = 'uploading'`)
      .bind(now, id)
      .run();
    return Number(changed.meta?.changes || 0) === 1 ? this.getArtifact(id) : null;
  }

  async createSubmission(row, maxPerHour) {
    const existing = await this.db
      .prepare("SELECT id, request_sha256 FROM submissions WHERE submitter_key_sha256 = ? AND idempotency_key = ?")
      .bind(row.submitterKeyHash, row.idempotencyKey)
      .first();
    if (existing) {
      return existing.request_sha256 === row.requestHash
        ? { kind: "replay", submission: await this.getSubmission(existing.id) }
        : { kind: "conflict" };
    }

    await this.db
      .prepare(`INSERT INTO submissions (
        id, status, image, argv_json, name, model_version, contact, notes,
        idempotency_key, request_sha256, submitter_key_sha256, created_at, updated_at,
        transport_type, artifact_id, artifact_object_key, artifact_archive_sha256,
        artifact_archive_size, artifact_image_id, progress_stage, progress_message,
        progress_completed, progress_total
      ) SELECT ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
        'queued', 'Waiting for a trusted runner.', 0, ?
      WHERE (
        SELECT COUNT(*) FROM submissions
        WHERE submitter_key_sha256 = ? AND created_at >= ?
      ) < ?
      ON CONFLICT (submitter_key_sha256, idempotency_key) DO NOTHING`)
      .bind(
        row.id,
        row.image,
        JSON.stringify(row.argv),
        row.name,
        row.modelVersion,
        row.contact,
        row.notes,
        row.idempotencyKey,
        row.requestHash,
        row.submitterKeyHash,
        row.now,
        row.now,
        row.transportType || "registry",
        row.artifact?.id || null,
        row.artifact?.objectKey || null,
        row.artifact?.archiveSha256 || null,
        row.artifact?.archiveSize || null,
        row.artifact?.imageId || null,
        row.progressTotal || 16,
        row.submitterKeyHash,
        row.now - 3600,
        maxPerHour,
      )
      .run();

    const stored = await this.db
      .prepare("SELECT id, request_sha256 FROM submissions WHERE submitter_key_sha256 = ? AND idempotency_key = ?")
      .bind(row.submitterKeyHash, row.idempotencyKey)
      .first();
    if (!stored) return { kind: "rate_limited" };
    if (stored.request_sha256 !== row.requestHash) return { kind: "conflict" };
    return { kind: stored.id === row.id ? "created" : "replay", submission: await this.getSubmission(stored.id) };
  }

  async getSubmission(id) {
    const row = await this.db.prepare("SELECT * FROM submissions WHERE id = ?").bind(id).first();
    return row ? deserialize(row) : null;
  }

  async listSubmissions({ status, model, limit, cursor = null }) {
    const clauses = [];
    const bindings = [];
    if (status) {
      clauses.push("status = ?");
      bindings.push(status);
    }
    if (model) {
      clauses.push("(name LIKE ? OR image LIKE ?)");
      bindings.push(`%${model}%`, `%${model}%`);
    }
    if (cursor) {
      clauses.push("(created_at < ? OR (created_at = ? AND id < ?))");
      bindings.push(cursor.createdAt, cursor.createdAt, cursor.id);
    }
    const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
    const result = await this.db
      .prepare(`SELECT * FROM submissions ${where} ORDER BY created_at DESC, id DESC LIMIT ?`)
      .bind(...bindings, limit + 1)
      .all();
    const rows = (result.results || []).map(deserialize);
    const hasMore = rows.length > limit;
    const page = rows.slice(0, limit);
    const last = page.at(-1);
    return { rows: page, nextCursor: hasMore && last ? { createdAt: last.createdAt, id: last.id } : null };
  }

  async operatorComparisons() {
    const images = await this.db
      .prepare("SELECT image FROM submissions GROUP BY image HAVING COUNT(*) > 1 LIMIT 10001")
      .all();
    const results = await this.db
      .prepare("SELECT result_sha256 FROM submissions WHERE result_sha256 IS NOT NULL GROUP BY result_sha256 HAVING COUNT(*) > 1 LIMIT 10001")
      .all();
    return {
      scope: "store_wide",
      truncated: (images.results || []).length > 10000 || (results.results || []).length > 10000,
      duplicateImages: new Set((images.results || []).map((row) => row.image)),
      duplicateResults: new Set((results.results || []).map((row) => row.result_sha256)),
    };
  }

  async addOperatorNote({ id, submissionId, verdict, note, createdAt, operatorKeyHash, actorId }) {
    await this.db
      .prepare(`INSERT INTO operator_notes (id, submission_id, verdict, note, created_at, operator_key_sha256, actor_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)`)
      .bind(id, submissionId, verdict, note, createdAt, operatorKeyHash, actorId)
      .run();
    return { id, submissionId, verdict, note, createdAt, actorId };
  }

  async listOperatorNotes(submissionId) {
    const result = await this.db
      .prepare("SELECT id, submission_id, verdict, note, created_at, actor_id FROM operator_notes WHERE submission_id = ? ORDER BY created_at ASC, id ASC")
      .bind(submissionId)
      .all();
    return (result.results || []).map((row) => ({
      id: row.id,
      submissionId: row.submission_id,
      verdict: row.verdict,
      note: row.note,
      createdAt: row.created_at,
      actorId: row.actor_id,
    }));
  }

  async leaseJob({ now, leaseExpiresAt, leaseTokenHash, predictionOverlaysRequired }) {
    await this.requeueExpired(now);

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const queued = await this.db
        .prepare("SELECT id, attempt FROM submissions WHERE status = 'queued' ORDER BY created_at, id LIMIT 1")
        .first();
      if (!queued) return null;
      await this.db
        .prepare("DELETE FROM prediction_overlays WHERE submission_id = ? AND attempt <= ?")
        .bind(queued.id, queued.attempt)
        .run();
      await this.db
        .prepare("DELETE FROM prediction_overlay_sets WHERE submission_id = ? AND attempt <= ?")
        .bind(queued.id, queued.attempt)
        .run();
      const changed = await this.db
        .prepare(`UPDATE submissions SET status = 'running', lease_token_sha256 = ?,
          lease_expires_at = ?, attempt = attempt + 1, prediction_overlays_required = ?,
          prediction_overlay_attempt = NULL, prediction_overlay_root_sha256 = NULL,
          started_at = COALESCE(started_at, ?), updated_at = ?,
          progress_stage = 'runner_started', progress_message = 'Trusted runner started.',
          progress_completed = 0
          WHERE id = ? AND status = 'queued'`)
        .bind(leaseTokenHash, leaseExpiresAt, predictionOverlaysRequired ? 1 : 0, now, now, queued.id)
        .run();
      if (Number(changed.meta?.changes || 0) === 1) return this.getSubmission(queued.id);
    }
    return null;
  }

  async updateProgress({ id, leaseTokenHash, stage, message, completed, total, now }) {
    const changed = await this.db.prepare(`UPDATE submissions
      SET progress_stage = ?, progress_message = ?, progress_completed = ?,
        progress_total = ?, updated_at = ?
      WHERE id = ? AND status = 'running' AND lease_token_sha256 = ? AND lease_expires_at >= ?`)
      .bind(stage, message, completed, total, now, id, leaseTokenHash, now)
      .run();
    return Number(changed.meta?.changes || 0) === 1 ? this.getSubmission(id) : null;
  }

  async requeueExpired(now) {
    const changed = await this.db
      .prepare(`UPDATE submissions SET status = 'queued', lease_token_sha256 = NULL,
        lease_expires_at = NULL, updated_at = ?, progress_stage = 'queued',
        progress_message = 'Runner lease expired; safely queued for retry.',
        progress_completed = 0
        WHERE status = 'running' AND lease_expires_at < ?`)
      .bind(now, now)
      .run();
    return Number(changed.meta?.changes || 0);
  }

  async stagePredictionOverlay({ id, leaseTokenHash, scenarioId, payloadJson, payloadSha256, byteCount, predictionCount, now }) {
    const submission = await this.db
      .prepare(`SELECT attempt FROM submissions
        WHERE id = ? AND status = 'running' AND lease_token_sha256 = ? AND lease_expires_at >= ?`)
      .bind(id, leaseTokenHash, now)
      .first();
    if (!submission) return { kind: "invalid_transition" };
    const changed = await this.db
      .prepare(`INSERT OR IGNORE INTO prediction_overlays (
        submission_id, attempt, scenario_id, payload_json, payload_sha256,
        byte_count, prediction_count, created_at
      ) SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (
        SELECT 1 FROM submissions
        WHERE id = ? AND attempt = ? AND status = 'running'
          AND lease_token_sha256 = ? AND lease_expires_at >= ?
      )`)
      .bind(
        id, submission.attempt, scenarioId, payloadJson, payloadSha256, byteCount, predictionCount, now,
        id, submission.attempt, leaseTokenHash, now,
      )
      .run();
    const stored = await this.db
      .prepare(`SELECT payload_sha256 FROM prediction_overlays
        WHERE submission_id = ? AND attempt = ? AND scenario_id = ?`)
      .bind(id, submission.attempt, scenarioId)
      .first();
    if (!stored) return { kind: "invalid_transition" };
    if (Number(changed.meta?.changes || 0) === 1) return { kind: "created" };
    return { kind: stored.payload_sha256 === payloadSha256 ? "replay" : "conflict" };
  }

  async stagedPredictionOverlays({ id, leaseTokenHash, now }) {
    const submission = await this.db
      .prepare(`SELECT attempt FROM submissions
        WHERE id = ? AND status = 'running' AND lease_token_sha256 = ? AND lease_expires_at >= ?`)
      .bind(id, leaseTokenHash, now)
      .first();
    if (!submission) return null;
    const result = await this.db
      .prepare(`SELECT scenario_id, payload_sha256, byte_count FROM prediction_overlays
        WHERE submission_id = ? AND attempt = ? ORDER BY scenario_id`)
      .bind(id, submission.attempt)
      .all();
    return { attempt: submission.attempt, rows: result.results || [] };
  }

  async sealPredictionOverlays({ id, attempt, leaseTokenHash, rootSha256, now }) {
    const changed = await this.db
      .prepare(`INSERT OR IGNORE INTO prediction_overlay_sets (submission_id, attempt, root_sha256, sealed_at)
        SELECT ?, ?, ?, ? WHERE EXISTS (
          SELECT 1 FROM submissions
          WHERE id = ? AND attempt = ? AND status = 'running'
            AND lease_token_sha256 = ? AND lease_expires_at >= ?
        )`)
      .bind(id, attempt, rootSha256, now, id, attempt, leaseTokenHash, now)
      .run();
    const stored = await this.db
      .prepare("SELECT root_sha256 FROM prediction_overlay_sets WHERE submission_id = ? AND attempt = ?")
      .bind(id, attempt)
      .first();
    if (!stored) return { kind: "invalid_transition" };
    if (Number(changed.meta?.changes || 0) === 1) return { kind: "created" };
    return { kind: stored.root_sha256 === rootSha256 ? "replay" : "conflict" };
  }

  async getPredictionOverlay(id, scenarioId) {
    const row = await this.db
      .prepare(`SELECT o.payload_json, o.payload_sha256
        FROM submissions s
        JOIN prediction_overlay_sets overlay_set
          ON overlay_set.submission_id = s.id
          AND overlay_set.attempt = s.prediction_overlay_attempt
          AND overlay_set.root_sha256 = s.prediction_overlay_root_sha256
        JOIN prediction_overlays o
          ON o.submission_id = s.id AND o.attempt = s.prediction_overlay_attempt
        WHERE s.id = ? AND s.status = 'succeeded'
          AND s.prediction_overlay_root_sha256 IS NOT NULL AND o.scenario_id = ?`)
      .bind(id, scenarioId)
      .first();
    return row ? { payload: JSON.parse(row.payload_json), sha256: row.payload_sha256 } : null;
  }

  async completeJob({ id, leaseTokenHash, status, report, resultSha256, error, now }) {
    const changed = await this.db
      .prepare(`UPDATE submissions SET status = ?, result_json = ?, result_sha256 = ?, error = ?, completed_at = ?,
        updated_at = ?, prediction_overlay_attempt = CASE WHEN ? = 'succeeded' THEN attempt ELSE NULL END,
        prediction_overlay_root_sha256 = CASE WHEN ? = 'succeeded' THEN (
          SELECT root_sha256 FROM prediction_overlay_sets
          WHERE submission_id = submissions.id AND attempt = submissions.attempt
        ) ELSE NULL END,
        lease_token_sha256 = NULL, lease_expires_at = NULL,
        progress_stage = ?, progress_message = ?,
        progress_completed = CASE WHEN ? = 'succeeded' THEN progress_total ELSE progress_completed END
        WHERE id = ? AND status = 'running' AND lease_token_sha256 = ? AND lease_expires_at >= ?
          AND (? != 'succeeded' OR prediction_overlays_required = 0 OR EXISTS (
            SELECT 1 FROM prediction_overlay_sets
            WHERE submission_id = submissions.id AND attempt = submissions.attempt
          ))`)
      .bind(
        status,
        report === null ? null : JSON.stringify(report),
        resultSha256,
        error,
        now,
        now,
        status,
        status,
        status,
        status === "succeeded" ? "Benchmark completed." : "Benchmark failed.",
        status,
        id,
        leaseTokenHash,
        now,
        status,
      )
      .run();
    return Number(changed.meta?.changes || 0) === 1 ? this.getSubmission(id) : null;
  }
}

function deserialize(row) {
  return {
    id: row.id,
    status: row.status,
    image: row.image,
    argv: JSON.parse(row.argv_json),
    name: row.name,
    modelVersion: row.model_version,
    contact: row.contact,
    notes: row.notes,
    attempt: row.attempt,
    result: row.result_json ? JSON.parse(row.result_json) : null,
    resultSha256: row.result_sha256 || null,
    error: row.error,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    startedAt: row.started_at,
    completedAt: row.completed_at,
    leaseExpiresAt: row.lease_expires_at,
    transportType: row.transport_type || "registry",
    artifact: row.artifact_id
      ? {
          id: row.artifact_id,
          objectKey: row.artifact_object_key,
          archiveSha256: row.artifact_archive_sha256,
          archiveSize: row.artifact_archive_size,
          imageId: row.artifact_image_id,
          compression: "gzip",
        }
      : null,
    progress: {
      stage: row.progress_stage || (row.status === "queued" ? "queued" : row.status),
      message: row.progress_message || null,
      completed: row.progress_completed || 0,
      total: row.progress_total || 16,
    },
    predictionOverlaysRequired: row.prediction_overlays_required === 1,
    predictionOverlayAttempt: row.prediction_overlay_attempt ?? null,
    predictionOverlayRootSha256: row.prediction_overlay_root_sha256 || null,
  };
}

function deserializeArtifact(row) {
  return {
    id: row.id,
    submitterKeyHash: row.submitter_key_sha256,
    objectKey: row.object_key,
    multipartUploadId: row.multipart_upload_id,
    status: row.status,
    archiveSha256: row.archive_sha256,
    archiveSize: row.archive_size,
    imageId: row.image_id,
    compression: row.compression,
    createdAt: row.created_at,
    completedAt: row.completed_at,
  };
}
