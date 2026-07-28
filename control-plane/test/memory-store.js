export class MemoryStore {
  constructor() {
    this.rows = new Map();
    this.notes = new Map();
    this.artifacts = new Map();
    this.artifactParts = new Map();
    this.createTail = Promise.resolve();
  }

  async health() {}

  async createArtifact(row, maxPerHour) {
    const recent = [...this.artifacts.values()].filter(
      (artifact) => artifact.submitterKeyHash === row.submitterKeyHash && artifact.createdAt >= row.now - 3600,
    );
    if (recent.length >= maxPerHour) return null;
    const artifact = {
      ...row,
      status: "uploading",
      compression: "gzip",
      createdAt: row.now,
      completedAt: null,
    };
    this.artifacts.set(row.id, artifact);
    return clone(artifact);
  }

  async getArtifact(id) {
    return this.artifacts.has(id) ? clone(this.artifacts.get(id)) : null;
  }

  async recordArtifactPart(part) {
    this.artifactParts.set(`${part.id}:${part.partNumber}`, clone(part));
    return this.listArtifactParts(part.id);
  }

  async listArtifactParts(id) {
    return [...this.artifactParts.values()]
      .filter((part) => part.id === id)
      .sort((left, right) => left.partNumber - right.partNumber)
      .map(clone);
  }

  async completeArtifact({ id, now }) {
    const artifact = this.artifacts.get(id);
    if (!artifact || artifact.status !== "uploading") return null;
    Object.assign(artifact, { status: "ready", completedAt: now });
    return clone(artifact);
  }

  async createSubmission(row, maxPerHour) {
    const operation = this.createTail.then(() => this.createSubmissionAtomic(row, maxPerHour));
    this.createTail = operation.catch(() => {});
    return operation;
  }

  createSubmissionAtomic(row, maxPerHour) {
    const existing = [...this.rows.values()].find(
      (item) => item.submitterKeyHash === row.submitterKeyHash && item.idempotencyKey === row.idempotencyKey,
    );
    if (existing) {
      return existing.requestHash === row.requestHash
        ? { kind: "replay", submission: clone(existing) }
        : { kind: "conflict" };
    }
    const recent = [...this.rows.values()].filter(
      (item) => item.submitterKeyHash === row.submitterKeyHash && item.createdAt >= row.now - 3600,
    );
    if (recent.length >= maxPerHour) return { kind: "rate_limited" };
    const stored = {
      ...row,
      status: "queued",
      attempt: 0,
      result: null,
      error: null,
      createdAt: row.now,
      updatedAt: row.now,
      startedAt: null,
      completedAt: null,
      leaseExpiresAt: null,
      leaseTokenHash: null,
      resultSha256: null,
      predictionOverlaysRequired: false,
      predictionOverlayAttempt: null,
      predictionOverlayRootSha256: null,
      transportType: row.transportType || "registry",
      artifact: row.artifact || null,
      progress: {
        stage: "queued",
        message: "Waiting for a trusted runner.",
        completed: 0,
        total: row.progressTotal || 16,
      },
    };
    this.rows.set(row.id, stored);
    return { kind: "created", submission: clone(stored) };
  }

  async getSubmission(id) {
    return this.rows.has(id) ? clone(this.rows.get(id)) : null;
  }

  async listSubmissions({ status, model, limit, cursor = null }) {
    const rows = [...this.rows.values()]
      .filter((row) => (!status || row.status === status) && (!model || row.name.includes(model) || row.image.includes(model)))
      .filter((row) => !cursor || row.createdAt < cursor.createdAt || (row.createdAt === cursor.createdAt && row.id < cursor.id))
      .sort((left, right) => right.createdAt - left.createdAt || right.id.localeCompare(left.id))
      .map(clone);
    const page = rows.slice(0, limit);
    const last = page.at(-1);
    return { rows: page, nextCursor: rows.length > limit && last ? { createdAt: last.createdAt, id: last.id } : null };
  }

  async operatorComparisons() {
    const imageCounts = new Map();
    const resultCounts = new Map();
    for (const row of this.rows.values()) {
      imageCounts.set(row.image, (imageCounts.get(row.image) || 0) + 1);
      if (row.resultSha256) resultCounts.set(row.resultSha256, (resultCounts.get(row.resultSha256) || 0) + 1);
    }
    return {
      scope: "store_wide",
      truncated: false,
      duplicateImages: new Set([...imageCounts].filter(([, count]) => count > 1).map(([image]) => image)),
      duplicateResults: new Set([...resultCounts].filter(([, count]) => count > 1).map(([hash]) => hash)),
    };
  }

  async addOperatorNote({ id, submissionId, verdict, note, createdAt, actorId }) {
    const stored = { id, submissionId, verdict, note, createdAt, actorId };
    this.notes.set(id, stored);
    return clone(stored);
  }

  async listOperatorNotes(submissionId) {
    return [...this.notes.values()]
      .filter((note) => note.submissionId === submissionId)
      .sort((left, right) => left.createdAt - right.createdAt || left.id.localeCompare(right.id))
      .map(clone);
  }

  async leaseJob({ now, leaseExpiresAt, leaseTokenHash, predictionOverlaysRequired }) {
    await this.requeueExpired(now);
    const queued = [...this.rows.values()]
      .filter((row) => row.status === "queued")
      .sort((left, right) => left.createdAt - right.createdAt || left.id.localeCompare(right.id))[0];
    if (!queued) return null;
    Object.assign(queued, {
      status: "running",
      attempt: queued.attempt + 1,
      startedAt: queued.startedAt || now,
      updatedAt: now,
      leaseExpiresAt,
      leaseTokenHash,
      predictionOverlaysRequired,
      predictionOverlayAttempt: null,
      predictionOverlayRootSha256: null,
      progress: {
        ...queued.progress,
        stage: "runner_started",
        message: "Trusted runner started.",
        completed: 0,
      },
    });
    for (const [key, item] of this.overlays || []) {
      if (item.id === queued.id && item.attempt !== queued.attempt) this.overlays.delete(key);
    }
    for (const key of this.overlaySets?.keys() || []) {
      if (key.startsWith(`${queued.id}:`) && key !== `${queued.id}:${queued.attempt}`) this.overlaySets.delete(key);
    }
    return clone(queued);
  }

  async updateProgress({ id, leaseTokenHash, stage, message, completed, total, now }) {
    const row = this.rows.get(id);
    if (!row || row.status !== "running" || row.leaseTokenHash !== leaseTokenHash || row.leaseExpiresAt < now) {
      return null;
    }
    row.progress = { stage, message, completed, total };
    row.updatedAt = now;
    return clone(row);
  }

  async requeueExpired(now) {
    let count = 0;
    for (const row of this.rows.values()) {
      if (row.status === "running" && row.leaseExpiresAt < now) {
        Object.assign(row, {
          status: "queued",
          leaseExpiresAt: null,
          leaseTokenHash: null,
          updatedAt: now,
          progress: {
            ...row.progress,
            stage: "queued",
            message: "Runner lease expired; safely queued for retry.",
            completed: 0,
          },
        });
        count += 1;
      }
    }
    return count;
  }

  async completeJob({ id, leaseTokenHash, status, report, resultSha256, error, now }) {
    const row = this.rows.get(id);
    if (!row || row.status !== "running" || row.leaseTokenHash !== leaseTokenHash || row.leaseExpiresAt < now) {
      return null;
    }
    const overlaySet = this.overlaySets?.get(`${id}:${row.attempt}`);
    if (status === "succeeded" && row.predictionOverlaysRequired && !overlaySet) return null;
    Object.assign(row, {
      status,
      result: report,
      resultSha256,
      error,
      completedAt: now,
      updatedAt: now,
      leaseTokenHash: null,
      leaseExpiresAt: null,
      predictionOverlayAttempt: status === "succeeded" ? row.attempt : null,
      predictionOverlayRootSha256: status === "succeeded" ? overlaySet?.rootSha256 || null : null,
      progress: {
        ...row.progress,
        stage: status === "succeeded" ? "completed" : "failed",
        message: status === "succeeded" ? "Benchmark completed." : "Benchmark failed.",
        completed: status === "succeeded" ? row.progress.total : row.progress.completed,
      },
    });
    return clone(row);
  }

  async stagePredictionOverlay({ id, leaseTokenHash, scenarioId, payloadJson, payloadSha256, byteCount, predictionCount, now }) {
    const row = this.rows.get(id);
    if (!row || row.status !== "running" || row.leaseTokenHash !== leaseTokenHash || row.leaseExpiresAt < now) {
      return { kind: "invalid_transition" };
    }
    this.overlays ||= new Map();
    const key = `${id}:${row.attempt}:${scenarioId}`;
    const existing = this.overlays.get(key);
    if (existing) return { kind: existing.payloadSha256 === payloadSha256 ? "replay" : "conflict" };
    this.overlays.set(key, { id, attempt: row.attempt, scenarioId, payloadJson, payloadSha256, byteCount, predictionCount });
    return { kind: "created" };
  }

  async stagedPredictionOverlays({ id, leaseTokenHash, now }) {
    const row = this.rows.get(id);
    if (!row || row.status !== "running" || row.leaseTokenHash !== leaseTokenHash || row.leaseExpiresAt < now) return null;
    return {
      attempt: row.attempt,
      rows: [...(this.overlays?.values() || [])]
        .filter((item) => item.id === id && item.attempt === row.attempt)
        .sort((left, right) => left.scenarioId.localeCompare(right.scenarioId))
        .map((item) => ({
          scenario_id: item.scenarioId,
          payload_sha256: item.payloadSha256,
          byte_count: item.byteCount,
        })),
    };
  }

  async sealPredictionOverlays({ id, attempt, leaseTokenHash, rootSha256, now }) {
    const row = this.rows.get(id);
    if (!row || row.attempt !== attempt || row.status !== "running" || row.leaseTokenHash !== leaseTokenHash || row.leaseExpiresAt < now) {
      return { kind: "invalid_transition" };
    }
    this.overlaySets ||= new Map();
    const key = `${id}:${attempt}`;
    const existing = this.overlaySets.get(key);
    if (existing) return { kind: existing.rootSha256 === rootSha256 ? "replay" : "conflict" };
    this.overlaySets.set(key, { rootSha256 });
    return { kind: "created" };
  }

  async getPredictionOverlay(id, scenarioId) {
    const row = this.rows.get(id);
    if (!row || row.status !== "succeeded" || !row.predictionOverlayRootSha256) return null;
    const overlay = this.overlays?.get(`${id}:${row.predictionOverlayAttempt}:${scenarioId}`);
    return overlay ? { payload: JSON.parse(overlay.payloadJson), sha256: overlay.payloadSha256 } : null;
  }
}

function clone(value) {
  return structuredClone(value);
}
