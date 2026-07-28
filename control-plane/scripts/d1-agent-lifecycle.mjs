const baseUrl = required("CVBENCH_API_BASE_URL").replace(/\/$/, "");
const submissionKey = required("CVBENCH_API_KEY");
const runnerToken = required("CVBENCH_RUNNER_TOKEN");
const archive = new TextEncoder().encode("cvbench local D1 and R2 agent lifecycle proof");
const archiveSha256 = await sha256(archive);
const imageId = `sha256:${"c".repeat(64)}`;

const artifactResponse = await fetch(`${baseUrl}/api/v1/artifacts`, {
  method: "POST",
  headers: {
    authorization: `Bearer ${submissionKey}`,
    "content-type": "application/json",
  },
  body: JSON.stringify({
    archive_sha256: archiveSha256,
    archive_size: archive.byteLength,
    image_id: imageId,
    compression: "gzip",
  }),
});
await assertStatus(artifactResponse, 201, "artifact create");
const artifact = await artifactResponse.json();

const partResponse = await fetch(`${baseUrl}/api/v1/artifacts/${artifact.id}/parts/1`, {
  method: "PUT",
  headers: {
    authorization: `Bearer ${submissionKey}`,
    "content-type": "application/octet-stream",
    "content-length": String(archive.byteLength),
  },
  body: archive,
});
await assertStatus(partResponse, 201, "artifact part");

const completeResponse = await fetch(`${baseUrl}/api/v1/artifacts/${artifact.id}/complete`, {
  method: "POST",
  headers: { authorization: `Bearer ${submissionKey}` },
});
await assertStatus(completeResponse, 200, "artifact complete");

const submissionResponse = await fetch(`${baseUrl}/api/v1/submissions`, {
  method: "POST",
  headers: {
    authorization: `Bearer ${submissionKey}`,
    "content-type": "application/json",
    "idempotency-key": `d1-agent-${crypto.randomUUID()}`,
  },
  body: JSON.stringify({
    artifact_id: artifact.id,
    argv: ["python", "-m", "tracker"],
    name: "D1 agent lifecycle proof",
    model_version: "local",
  }),
});
await assertStatus(submissionResponse, 201, "submission create");
const submission = await submissionResponse.json();
assert(submission.transport?.type === "uploaded_oci", "submission lost uploaded transport");

const leaseResponse = await fetch(`${baseUrl}/api/v1/internal/leases`, {
  method: "POST",
  headers: { authorization: `Bearer ${runnerToken}` },
});
await assertStatus(leaseResponse, 200, "lease");
const leased = await leaseResponse.json();
assert(leased.submission.id === submission.id, "leased a different local submission");

const downloadResponse = await fetch(`${baseUrl}/api/v1/internal/submissions/${submission.id}/artifact`, {
  headers: { authorization: `Bearer ${runnerToken}` },
});
await assertStatus(downloadResponse, 200, "artifact download");
assert(downloadResponse.headers.get("x-cvbench-archive-sha256") === archiveSha256, "archive hash header changed");
assert(downloadResponse.headers.get("x-cvbench-image-id") === imageId, "image ID header changed");
assert(equalBytes(new Uint8Array(await downloadResponse.arrayBuffer()), archive), "downloaded archive changed");

const progressResponse = await fetch(`${baseUrl}/api/v1/internal/submissions/${submission.id}/progress`, {
  method: "POST",
  headers: { authorization: `Bearer ${runnerToken}`, "content-type": "application/json" },
  body: JSON.stringify({
    lease_token: leased.lease.token,
    stage: "image_load",
    message: "D1 lifecycle image load proof.",
    completed: 0,
    total: 16,
  }),
});
await assertStatus(progressResponse, 200, "progress");

const callbackResponse = await fetch(`${baseUrl}/api/v1/internal/submissions/${submission.id}/result`, {
  method: "POST",
  headers: { authorization: `Bearer ${runnerToken}`, "content-type": "application/json" },
  body: JSON.stringify({
    status: "failed",
    lease_token: leased.lease.token,
    error: "Intentional lifecycle proof; no model was executed.",
  }),
});
await assertStatus(callbackResponse, 200, "terminal callback");
const terminal = await callbackResponse.json();
assert(terminal.status === "failed", "submission did not reach its terminal state");
assert(terminal.agent_feedback?.verdict === "fix_and_retry", "terminal agent feedback is missing");

console.log(JSON.stringify({
  artifact_id: artifact.id,
  submission_id: submission.id,
  transport: submission.transport.type,
  terminal_status: terminal.status,
  feedback_schema: terminal.agent_feedback.schema_version,
}));

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function equalBytes(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function assertStatus(response, expected, operation) {
  if (response.status !== expected) {
    throw new Error(`${operation} returned ${response.status}: ${await response.text()}`);
  }
}
