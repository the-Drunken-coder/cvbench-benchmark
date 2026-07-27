import { readFile } from "node:fs/promises";

const baseUrl = required("CVBENCH_API_BASE_URL").replace(/\/$/, "");
const submissionKey = required("CVBENCH_API_KEY");
const runnerToken = required("CVBENCH_RUNNER_TOKEN");
const operatorReadToken = required("CVBENCH_OPERATOR_READ_TOKEN");
const operatorWriteToken = required("CVBENCH_OPERATOR_WRITE_TOKEN");
const operatorSecondWriteToken = required("CVBENCH_OPERATOR_SECOND_WRITE_TOKEN");
const expectedActorId = required("CVBENCH_OPERATOR_ACTOR_ID");
const expectedSecondActorId = required("CVBENCH_OPERATOR_SECOND_ACTOR_ID");
const report = JSON.parse(await readFile(required("CVBENCH_REPORT_PATH"), "utf8"));
const digest = "b".repeat(64);
const idempotencyKey = `safe-baseline-${crypto.randomUUID()}`;

assert(report.outcome?.status === "completed", "baseline report must be scored and completed");
assert(Number(report.metrics?.sample_counts?.matches) > 0, "baseline report must contain scored matches");

const createdResponse = await fetch(`${baseUrl}/api/v1/submissions`, {
  method: "POST",
  headers: {
    authorization: `Bearer ${submissionKey}`,
    "content-type": "application/json",
    "idempotency-key": idempotencyKey,
  },
  body: JSON.stringify({
    image: `ghcr.io/cvbench/safe-baseline@sha256:${digest}`,
    argv: ["python", "-m", "cvbench.examples.good_tracker"],
    name: "CVBench safe baseline lifecycle proof",
    model_version: "1.0.0",
  }),
});
await assertStatus(createdResponse, 201, "create");
const created = await createdResponse.json();

const replayResponse = await fetch(`${baseUrl}/api/v1/submissions`, {
  method: "POST",
  headers: {
    authorization: `Bearer ${submissionKey}`,
    "content-type": "application/json",
    "idempotency-key": idempotencyKey,
  },
  body: JSON.stringify({
    image: `ghcr.io/cvbench/safe-baseline@sha256:${digest}`,
    argv: ["python", "-m", "cvbench.examples.good_tracker"],
    name: "CVBench safe baseline lifecycle proof",
    model_version: "1.0.0",
  }),
});
await assertStatus(replayResponse, 200, "idempotent replay");
assert((await replayResponse.json()).id === created.id, "idempotent replay changed submission ID");

const leaseResponse = await fetch(`${baseUrl}/api/v1/internal/leases`, {
  method: "POST",
  headers: { authorization: `Bearer ${runnerToken}` },
});
await assertStatus(leaseResponse, 200, "lease");
const leased = await leaseResponse.json();
assert(leased.submission.id === created.id, "leased a different submission");

for (const scenarioId of leased.submission.benchmark.scenario_ids) {
  const overlayResponse = await fetch(
    `${baseUrl}/api/v1/internal/submissions/${created.id}/prediction-overlays/${scenarioId}`,
    {
      method: "PUT",
      headers: {
        authorization: `Bearer ${runnerToken}`,
        "content-type": "application/json",
        "x-cvbench-lease-token": leased.lease.token,
      },
      body: JSON.stringify({
        schema_version: "cvbench.prediction-overlay/v1",
        state: "complete",
        scenario_id: scenarioId,
        width: 1,
        height: 1,
        frame_count: 1,
        frames: [{ frame_index: 0, source_timestamp_ns: 0, objects: [] }],
        summary: { prediction_count: 0 },
      }),
    },
  );
  await assertStatus(overlayResponse, 201, `overlay ${scenarioId}`);
}
const sealResponse = await fetch(
  `${baseUrl}/api/v1/internal/submissions/${created.id}/prediction-overlays/complete`,
  {
    method: "POST",
    headers: { authorization: `Bearer ${runnerToken}`, "content-type": "application/json" },
    body: JSON.stringify({ lease_token: leased.lease.token }),
  },
);
await assertStatus(sealResponse, 201, "overlay seal");

const callbackResponse = await fetch(`${baseUrl}/api/v1/internal/submissions/${created.id}/result`, {
  method: "POST",
  headers: { authorization: `Bearer ${runnerToken}`, "content-type": "application/json" },
  body: JSON.stringify({ status: "succeeded", lease_token: leased.lease.token, report }),
});
await assertStatus(callbackResponse, 200, "callback");

const publicResponse = await fetch(`${baseUrl}/api/v1/submissions/${created.id}`);
assert(publicResponse.status === 200, `public read returned ${publicResponse.status}`);
const completed = await publicResponse.json();
assert(completed.status === "succeeded", "submission did not reach succeeded");
assert(completed.result.scores.sample_counts.matches > 0, "public result lost scored matches");
assert(completed.result.audit_evidence === undefined, "public submission exposed detailed audit evidence");
assert(completed.result.diagnostics === undefined, "public submission exposed diagnostics");

const operatorHeaders = { authorization: `Bearer ${operatorReadToken}` };
const operatorResponse = await fetch(`${baseUrl}/api/v1/operator/jobs/${created.id}/audit`, { headers: operatorHeaders });
await assertStatus(operatorResponse, 200, "operator audit");
const audit = await operatorResponse.json();
assert(audit.automatic_disqualification === false, "audit flags must not disqualify automatically");
assert(audit.fairness.explainable_evidence === true, "completed audit lost explainable evidence");
const evidenceResponse = await fetch(`${baseUrl}/api/v1/operator/jobs/${created.id}/evidence`, { headers: operatorHeaders });
await assertStatus(evidenceResponse, 200, "operator evidence");
const evidence = await evidenceResponse.json();
assert(evidence.audit_evidence?.schema_version === "cvbench.audit/v1", "audit evidence was not retrieved");
assert(evidence.audit_evidence.frame_samples?.length > 0, "operator evidence lost frame samples");
assert(evidence.audit_evidence.score_explanation?.coverage_denominators, "operator evidence lost denominator explanations");
assert(evidence.audit_evidence.flags?.length > 0, "operator evidence lost audit flags");
assert(evidence.audit_evidence.false_track_segments, "operator evidence lost false-track evidence");
assert(
  evidence.audit_evidence.neutral_ignored_predictions?.count ===
    report.metrics.sample_counts.neutral_ignored_predictions,
  "metric neutral count does not reconcile with operator evidence",
);
const neutralPredictions = evidence.audit_evidence.frame_samples.flatMap((sample) =>
  sample.predictions
    .filter((prediction) => prediction.neutral_ignored)
    .map((prediction) => ({
      sequence_id: sample.sequence_id,
      source_timestamp_ns: sample.source_timestamp_ns,
      track_id: prediction.track_id,
    })),
);
const falseTrackSegments = evidence.audit_evidence.false_track_segments;
assert(
  neutralPredictions.every((prediction) => {
    const segment = falseTrackSegments.find(
      (candidate) =>
        candidate.sequence_id === prediction.sequence_id && candidate.track_id === prediction.track_id,
    );
    return (
      !segment ||
      !(
        segment.start_timestamp_ns <= prediction.source_timestamp_ns &&
        prediction.source_timestamp_ns <= segment.end_timestamp_ns
      ) ||
      segment.neutral_ignored_timestamps_ns?.includes(prediction.source_timestamp_ns)
    );
  }),
  "neutral predictions appeared as scored false tracks",
);
assert(
  evidence.audit_evidence.score_explanation.scoreable_target_denominator ===
    report.metrics.acquisition.total_eligible_targets,
  "scoreable target denominator does not reconcile with metrics",
);
assert(
  evidence.audit_evidence.score_explanation.component_counts.localization ===
    report.metrics.localization.sample_count,
  "localization component does not reconcile with operator evidence",
);
assert(
  evidence.audit_evidence.false_track_segment_count === report.metrics.false_detections.track_births,
  "false-track component does not reconcile with operator evidence",
);
const noteResponse = await fetch(`${baseUrl}/api/v1/operator/jobs/${created.id}/notes`, {
  method: "POST",
  headers: { authorization: `Bearer ${operatorWriteToken}`, "content-type": "application/json" },
  body: JSON.stringify({ verdict: "accepted", note: "D1 lifecycle audit evidence retrieved." }),
});
await assertStatus(noteResponse, 201, "operator adjudication note");
const note = await noteResponse.json();
assert(note.actorId === expectedActorId, "operator note has the wrong actor attribution");
const secondNoteResponse = await fetch(`${baseUrl}/api/v1/operator/jobs/${created.id}/notes`, {
  method: "POST",
  headers: { authorization: `Bearer ${operatorSecondWriteToken}`, "content-type": "application/json" },
  body: JSON.stringify({ verdict: "needs_review", note: "Second actor attribution proof." }),
});
await assertStatus(secondNoteResponse, 201, "second operator adjudication note");
const secondNote = await secondNoteResponse.json();
assert(secondNote.actorId === expectedSecondActorId, "second operator note has the wrong actor attribution");
assert(note.actorId !== secondNote.actorId, "adjudicator credentials must map to distinct actors");

console.log(JSON.stringify({
  submission_id: completed.id,
  status: completed.status,
  matched_samples: completed.result.scores.sample_counts.matches,
  benchmark_outcome: completed.result.outcome.status,
  audit_schema: evidence.audit_evidence.schema_version,
  flagged_review_aids: audit.flags.filter((flag) => flag.status === "flagged").map((flag) => flag.id),
  actor_ids: [note.actorId, secondNote.actorId],
}));

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function assertStatus(response, expected, operation) {
  if (response.status !== expected) {
    throw new Error(`${operation} returned ${response.status}: ${await response.text()}`);
  }
}
