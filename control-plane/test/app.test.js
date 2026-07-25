import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";

import { canonicalJson, createApp, PUBLIC_BENCHMARK } from "../src/app.js";
import { MemoryStore } from "./memory-store.js";

const SUBMISSION_KEY = "submission-key-with-enough-entropy";
const RUNNER_TOKEN = "runner-token-with-enough-entropy";
const OPERATOR_READ_TOKEN = "operator-read-token-with-enough-entropy";
const OPERATOR_WRITE_TOKEN = "operator-alice-write-token-with-enough-entropy";
const OPERATOR_SECOND_WRITE_TOKEN = "operator-bob-write-token-with-enough-entropy";
const IMAGE = `ghcr.io/example/tracker@sha256:${"a".repeat(64)}`;
let app;
let store;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

beforeEach(() => {
  store = new MemoryStore();
  app = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: {
      "operator/alice": OPERATOR_WRITE_TOKEN,
      "operator/bob": OPERATOR_SECOND_WRITE_TOKEN,
    },
    maxSubmissionsPerHour: 2,
    leaseSeconds: 3000,
  });
});

test("health and machine-readable metadata are public", async () => {
  assert.equal((await request("/api/v1/health")).status, 200);
  const contract = await jsonRequest("/api/v1/contract");
  assert.equal(contract.container.network, "disabled");
  assert.match(contract.container.image, /sha256/);
  assert.match(contract.container.filesystem, /no extra mounts and no Docker socket/);
  assert.match(contract.benchmark.temporal_support, /multiple processes/);
  assert.equal(contract.benchmark.id, "public-whole-system-tracking");
  assert.equal(contract.benchmark.version, "2.0.0");
  assert.equal(contract.benchmark.scenario_count, 16);
  assert.deepEqual(contract.benchmark.scenario_ids, PUBLIC_BENCHMARK.scenario_ids);
  assert.match(contract.benchmark.selection, /Every public v1 submission/);
  assert.equal(contract.benchmark.replay_profile, "native");
  assert.equal(contract.benchmark.replay_rate, 1);
  assert.match(contract.benchmark.timing_compute.source_time, /immutable/);
  assert.match(contract.benchmark.timing_compute.fairness, /no hidden composite/);
  assert.match(contract.submission.terminology, /packaging, reproducibility, and security boundary/);
  assert.match(contract.submission.compatibility_names.model_version, /system version/);
  const openapi = await jsonRequest("/api/v1/openapi.json");
  assert.equal(openapi.openapi, "3.1.0");
  assert.match(openapi.info.description, /complete vision system/);
  assert.deepEqual(openapi["x-cvbench-public-benchmark"], PUBLIC_BENCHMARK);
  assert.match(openapi.components.schemas.CreateSubmission.properties.model_version.description, /submitted system version/);
  assert.ok(openapi.components.schemas.TimingComputeSummary);
  assert.equal(openapi.components.schemas.TimingComputeSummary.additionalProperties, false);
  assert.deepEqual(
    openapi.components.schemas.TimingComputeSummary.properties.native_source_offset_p95_ms,
    { type: ["number", "null"] },
  );
  const reportSchema = JSON.parse(await readFile(path.join(ROOT, "schemas/report-v1.schema.json"), "utf8"));
  const timingSchema = JSON.parse(await readFile(path.join(ROOT, "schemas/timing-compute-v1.schema.json"), "utf8"));
  reportSchema.properties.timing = { $ref: "#/components/schemas/TimingComputeV1" };
  assert.deepEqual(openapi.components.schemas.ReportV1, reportSchema);
  assert.deepEqual(openapi.components.schemas.TimingComputeV1, timingSchema);
  assert.ok(openapi.paths["/api/v1/internal/submissions/{id}/result"]);
  assert.ok(openapi.components.securitySchemes.operatorReadKey);
  assert.ok(openapi.components.securitySchemes.operatorAdjudicatorKey);
});

test("public benchmark descriptor exactly matches its versioned manifest", async () => {
  const benchmarkPath = path.join(ROOT, PUBLIC_BENCHMARK.manifest);
  const benchmark = parseYaml(await readFile(benchmarkPath, "utf8"));
  const scenarioIds = [];
  for (const relative of benchmark.scenarios) {
    const scenario = parseYaml(await readFile(path.resolve(path.dirname(benchmarkPath), relative), "utf8"));
    scenarioIds.push(scenario.id);
  }
  assert.equal(benchmark.id, PUBLIC_BENCHMARK.id);
  assert.equal(benchmark.version, PUBLIC_BENCHMARK.version);
  assert.equal(benchmark.input.replay_profile, PUBLIC_BENCHMARK.replay_profile);
  assert.equal({ "quarter-speed": 0.25, "half-speed": 0.5, native: 1 }[benchmark.input.replay_profile], PUBLIC_BENCHMARK.replay_rate);
  assert.deepEqual(scenarioIds, PUBLIC_BENCHMARK.scenario_ids);
  assert.equal(scenarioIds.length, PUBLIC_BENCHMARK.scenario_count);
  assert.equal(new Set(scenarioIds).size, scenarioIds.length);
});

test("catalog assets keep honest status, MIME, and cache semantics", async () => {
  const knownFrame = `${"a".repeat(64)}.jpg`;
  const assetApp = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: { "operator/alice": OPERATOR_WRITE_TOKEN },
    assets: {
      async fetch(assetRequest) {
        const pathname = new URL(assetRequest.url).pathname;
        if (pathname === "/scenario-catalog/v1/catalog.json") {
          if (assetRequest.headers.has("if-none-match")) return new Response(null, { status: 304, headers: { etag: '"catalog"' } });
          return new Response('{"scenario_count":16}', { headers: { "cache-control": "public, max-age=0, must-revalidate", "content-type": "application/json; charset=utf-8", etag: '"catalog"' } });
        }
        if (pathname === `/scenario-catalog/v1/assets/sha256/${knownFrame}`) {
          return new Response("jpeg", { headers: { "cache-control": "public, max-age=31556952, immutable", "content-type": "image/jpeg" } });
        }
        return new Response("<!doctype html><title>not found</title>", {
          status: 404,
          headers: { "cache-control": "public, max-age=31556952, immutable", "content-type": "text/html; charset=utf-8" },
        });
      },
    },
  });
  const catalog = await requestFor(assetApp, "/scenario-catalog/v1/catalog.json");
  assert.equal(catalog.status, 200);
  assert.match(catalog.headers.get("content-type"), /application\/json/);
  const revalidated = await requestFor(assetApp, "/scenario-catalog/v1/catalog.json", { headers: { "if-none-match": '"catalog"' } });
  assert.equal(revalidated.status, 304);
  const frame = await requestFor(assetApp, `/scenario-catalog/v1/assets/sha256/${knownFrame}`);
  assert.equal(frame.status, 200);
  assert.equal(frame.headers.get("content-type"), "image/jpeg");
  assert.match(frame.headers.get("cache-control"), /immutable/);

  const missingJson = await requestFor(assetApp, "/scenario-catalog/v1/scenarios/not-present.json");
  assert.equal(missingJson.status, 404);
  assert.match(missingJson.headers.get("content-type"), /application\/json/);
  assert.doesNotMatch(missingJson.headers.get("cache-control"), /immutable/);
  assert.equal((await missingJson.json()).error.code, "not_found");
  const missingFrame = await requestFor(assetApp, `/scenario-catalog/v1/assets/sha256/${"b".repeat(64)}.jpg`);
  assert.equal(missingFrame.status, 404);
  assert.equal(missingFrame.headers.get("content-type"), "image/jpeg");
  assert.doesNotMatch(missingFrame.headers.get("cache-control"), /immutable/);
  assert.equal(await missingFrame.text(), "");
});

test("submission requires authentication and strict immutable input", async () => {
  assert.equal((await submit(validBody(), "idem-key-0001", "wrong")).status, 401);
  assert.equal((await submit({ ...validBody(), image: "ghcr.io/example/tracker:latest" }, "idem-key-0001")).status, 422);
  assert.equal((await submit({ ...validBody(), command: "curl bad | sh" }, "idem-key-0001")).status, 422);
  assert.equal((await submit({ ...validBody(), argv: "python tracker.py" }, "idem-key-0001")).status, 422);
  assert.equal((await submit(validBody(), "short")).status, 400);
});

test("submission create, public read, idempotent replay, lease, and scored result lifecycle", async () => {
  const createdResponse = await submit(validBody(), "baseline-safe-0001");
  assert.equal(createdResponse.status, 201);
  const created = await createdResponse.json();
  assert.equal(created.status, "queued");
  assert.deepEqual(created.benchmark, PUBLIC_BENCHMARK);
  assert.equal(created.contact, undefined);

  const replayResponse = await submit(validBody(), "baseline-safe-0001");
  assert.equal(replayResponse.status, 200);
  assert.equal(replayResponse.headers.get("idempotency-replayed"), "true");
  assert.equal((await replayResponse.json()).id, created.id);

  const conflict = await submit({ ...validBody(), model_version: "2" }, "baseline-safe-0001");
  assert.equal(conflict.status, 409);

  const publicQueued = await jsonRequest(`/api/v1/submissions/${created.id}`);
  assert.equal(publicQueued.model.image, IMAGE);
  assert.equal(publicQueued.status, "queued");

  assert.equal((await lease("wrong")).status, 401);
  const leaseResponse = await lease();
  assert.equal(leaseResponse.status, 200);
  const leased = await leaseResponse.json();
  assert.equal(leased.submission.id, created.id);
  assert.equal(leased.submission.attempt, 1);
  assert.deepEqual(leased.submission.benchmark, PUBLIC_BENCHMARK);
  assert.equal(leased.lease.max_result_bytes, 1024 * 1024);
  assert.equal((await lease()).status, 204);

  const badCallback = await result(created.id, { status: "succeeded", lease_token: "x".repeat(64), report: scoredReport() });
  assert.equal(badCallback.status, 409);
  const completedResponse = await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
  });
  assert.equal(completedResponse.status, 200);
  const completed = await completedResponse.json();
  assert.equal(completed.status, "succeeded");
  assert.deepEqual(completed.benchmark, PUBLIC_BENCHMARK);
  assert.equal(completed.result.scores.sample_counts.matches, 12);
  assert.equal(completed.result.scores.cpu_time_seconds, 15);
  assert.equal(completed.result.scores.teardown_seconds, 0.1);
  assert.equal(completed.result.scores.accounting_complete, true);
  assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "succeeded");
  const operator = await jsonRequest(`/api/v1/operator/jobs/${created.id}`, {
    headers: { authorization: `Bearer ${OPERATOR_READ_TOKEN}` },
  });
  assert.deepEqual(operator.raw_result.runner, scoredReport().runner);
  assert.equal((await result(created.id, { status: "failed", lease_token: leased.lease.token, error: "late" })).status, 409);
});

test("runner callbacks must match the fixed public benchmark assignment", async () => {
  const created = await (await submit(validBody(), "wrong-suite-0001")).json();
  const leased = await (await lease()).json();
  const report = scoredReport();
  report.benchmark = { id: "persistent-target-tracking", version: "1.0.0" };
  const response = await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report,
  });
  assert.equal(response.status, 422);
  assert.match((await response.json()).error.message, /assigned public suite/);
});

test("successful callbacks reject empty, incomplete, and ineligible reports", async () => {
  const created = await (await submit(validBody(), "strict-report-0001")).json();
  const leased = await (await lease()).json();
  for (const [suffix, mutate, pattern] of [
    ["empty", () => ({}), /schema_version/],
    ["missing-cpu", (report) => { report.resources.cpu_time_seconds = null; return report; }, /cgroup axes/],
    ["missing-runner", (report) => { delete report.runner; return report; }, /trusted-runner metadata/],
    ["unfinished", (report) => {
      Object.assign(report.outcome, { status: "failed", exit_code: 1, errors: ["failed"], crashed: true });
      return report;
    }, /completed report outcome/],
    ["ineligible", (report) => { report.leaderboard.eligible = false; return report; }, /must be eligible/],
    ["no-class", (report) => { report.leaderboard.class_id = null; return report; }, /non-null class/],
  ]) {
    const response = await result(created.id, {
      status: "succeeded",
      lease_token: leased.lease.token,
      report: mutate(scoredReport()),
    });
    assert.equal(response.status, 422, suffix);
    assert.match((await response.json()).error.message, pattern, suffix);
  }
});

test("successful callbacks require internally consistent external cgroup evidence", async () => {
  const created = await (await submit(validBody(), "cgroup-evidence-0001")).json();
  const leased = await (await lease()).json();
  const probes = [
    ["empty samples", (report) => { report.resources.over_time = []; report.resources.sample_count = 0; }],
    ["fake sample source", (report) => { report.resources.over_time[0].accounting_source = "submitted_process"; }],
    ["missing final sample", (report) => { delete report.resources.over_time.at(-1).final_cumulative; }],
    ["multiple final samples", (report) => { report.resources.over_time[0].final_cumulative = true; }],
    ["false aggregate", (report) => { report.resources.cpu_time_seconds = 1; }],
    ["false availability", (report) => { report.resources.accounting_availability.disk_io = false; }],
  ];
  for (const [name, mutate] of probes) {
    const report = scoredReport();
    mutate(report);
    const response = await result(created.id, {
      status: "succeeded",
      lease_token: leased.lease.token,
      report,
    });
    assert.equal(response.status, 422, name);
    assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "running", name);
  }
  assert.equal((await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
  })).status, 200);
});

test("successful callbacks are bound to the leased image digest and argv", async () => {
  const created = await (await submit(validBody(), "lease-identity-0001")).json();
  const leased = await (await lease()).json();
  const otherImage = `ghcr.io/example/other@sha256:${"f".repeat(64)}`;
  const probes = [
    ["system command", (report) => { report.system.command = ["python", "-m", "other"]; }],
    ["provenance command", (report) => { report.provenance.system_command = ["python", "-m", "other"]; }],
    ["configured image", (report) => { report.runtime_isolation.image_identity.configured_reference = otherImage; }],
    ["resolved image", (report) => { report.outcome.resolved_image = otherImage; }],
  ];
  for (const [name, mutate] of probes) {
    const report = scoredReport();
    mutate(report);
    const response = await result(created.id, {
      status: "succeeded",
      lease_token: leased.lease.token,
      report,
    });
    assert.equal(response.status, 422, name);
    assert.match((await response.json()).error.message, /leased submission/, name);
    assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "running", name);
  }
  assert.equal((await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
  })).status, 200);
});

test("successful callbacks enforce the complete recursively strict published report contract", async () => {
  const created = await (await submit(validBody(), "complete-report-contract-0001")).json();
  const leased = await (await lease()).json();
  const probes = [
    ["reviewer HTTP-200 reproduction", (report) => {
      for (const key of ["ranking_method", "composite_score", "compute_tier", "completion_tier", "comparison_rule"]) {
        delete report.leaderboard[key];
      }
    }],
    ["missing timing", (report) => { delete report.timing.durations.completion_seconds; }],
    ["missing delivery", (report) => { delete report.timing.delivery.sender_call_ms; }],
    ["missing output", (report) => { delete report.timing.output.late_output_policy; }],
    ["missing clock", (report) => { delete report.timing.clocks.delivery; }],
    ["missing fingerprint", (report) => { delete report.provenance.comparison_fingerprint; }],
    ["missing eligibility", (report) => { delete report.leaderboard.eligible; }],
    ["missing isolation", (report) => { delete report.runtime_isolation.network_mode; }],
    ["nested extra", (report) => { report.leaderboard.raw_axes.efficiency.unpriced_sleep_credit = 1; }],
  ];
  for (const [name, mutate] of probes) {
    const report = scoredReport();
    mutate(report);
    const response = await result(created.id, {
      status: "succeeded",
      lease_token: leased.lease.token,
      report,
    });
    assert.equal(response.status, 422, name);
    assert.match((await response.json()).error.message, /violates cvbench\.report\/v1/, name);
    assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "running", name);
  }
  assert.equal((await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
    error: "opposite status field",
  })).status, 422);
  assert.equal((await result(created.id, {
    status: "failed",
    lease_token: leased.lease.token,
    error: "failed",
    report: scoredReport(),
  })).status, 422);
  assert.equal((await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
  })).status, 200);
});

test("runner callbacks cannot place another timing class in the native leaderboard", async () => {
  const created = await (await submit(validBody(), "wrong-timing-0001")).json();
  const leased = await (await lease()).json();
  const report = scoredReport();
  report.provenance.replay_profile = "half-speed";
  report.provenance.replay_rate = 0.5;
  report.leaderboard.replay_class = "half-speed";
  const response = await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report,
  });
  assert.equal(response.status, 422);
  assert.match((await response.json()).error.message, /leaderboard|native public leaderboard class/);
});

test("failed jobs require a bounded error and valid running lease", async () => {
  const created = await (await submit(validBody(), "failed-job-0001")).json();
  const leased = await (await lease()).json();
  assert.equal((await result(created.id, { status: "failed", lease_token: leased.lease.token })).status, 422);
  const response = await result(created.id, { status: "failed", lease_token: leased.lease.token, error: "container failed readiness" });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).error, "container failed readiness");
});

test("result callback payloads cannot exceed the advertised budget", async () => {
  const created = await (await submit(validBody(), "oversized-result-0001")).json();
  const leased = await (await lease()).json();
  const response = await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: { diagnostics: { sut_stderr: ["x".repeat(1024 * 1024)] } },
  });
  assert.equal(response.status, 413);
  assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "running");
});

test("successful callbacks reject unknown Docker verification claims", async () => {
  const created = await (await submit(validBody(), "unknown-isolation-0001")).json();
  const leased = await (await lease()).json();
  const report = scoredReport({
    status: "verification_failed",
    future_frame_isolation: null,
    ground_truth_access: null,
    repository_access: null,
    media_access: null,
    mounts: null,
    network_mode: null,
    image_identity_verified: null,
    container_user_alignment_verified: null,
  });
  const callback = await result(created.id, { status: "succeeded", lease_token: leased.lease.token, report });
  assert.equal(callback.status, 422);
  assert.match((await callback.json()).error.message, /fully verified Docker isolation/);
});

test("operator API is separate from public and runner credentials", async () => {
  const created = await (await submit(validBody(), "operator-job-0001")).json();
  assert.equal((await request("/api/v1/operator/jobs")).status, 401);
  assert.equal((await request("/api/v1/operator/jobs", { headers: { authorization: `Bearer ${RUNNER_TOKEN}` } })).status, 401);
  assert.equal((await request("/api/v1/operator/jobs", { headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}` } })).status, 401);

  const preflight = await (await request(`/api/v1/operator/jobs/${created.id}`, { headers: { authorization: `Bearer ${OPERATOR_READ_TOKEN}` } })).json();
  assert.equal(preflight.job.diagnostics.duplicate_result_fingerprint, "unknown");
  const leased = await (await lease()).json();
  await result(created.id, { status: "succeeded", lease_token: leased.lease.token, report: scoredReport() });
  const headers = { authorization: `Bearer ${OPERATOR_READ_TOKEN}` };
  const list = await (await request("/api/v1/operator/jobs?status=succeeded", { headers })).json();
  assert.equal(list.schema_version, "cvbench.operator/v1");
  assert.equal(list.jobs[0].model.image, IMAGE);
  assert.equal(list.jobs[0].diagnostics.failure_reason, null);
  assert.equal(list.jobs[0].diagnostics.comparison_scope, "store_wide");
  assert.equal(list.jobs[0].diagnostics.duplicate_model_fingerprint, "clear");
  const filtered = await (await request("/api/v1/operator/jobs?model=does-not-exist", { headers })).json();
  assert.equal(filtered.jobs.length, 0);
  const detail = await (await request(`/api/v1/operator/jobs/${created.id}`, { headers })).json();
  assert.equal(detail.raw_result.metrics.sample_counts.matches, 12);
  assert.equal(detail.raw_result.diagnostics.sut_stderr[0], "<script>throw new Error('untrusted')</script>");
  assert.equal(detail.raw_result.audit_evidence.schema_version, "cvbench.audit/v1");
  assert.equal(detail.job.diagnostics.duplicate_result_fingerprint, "clear");
  const audit = await (await request(`/api/v1/operator/jobs/${created.id}/audit`, { headers })).json();
  assert.equal(audit.automatic_disqualification, false);
  assert.equal(audit.fairness.explainable_evidence, true);
  assert.ok(audit.flags.some((flag) => flag.id === "false_track"));
  assert.equal(audit.score_components.sample_counts.matches, 12);
  const evidence = await (await request(`/api/v1/operator/jobs/${created.id}/evidence`, { headers })).json();
  assert.equal(evidence.audit_evidence.frame_samples[0].matches[0].target_id, "target-1");
  assert.equal(evidence.audit_evidence.score_explanation.coverage_denominators.observed_coverage, 1);
  assert.equal(evidence.audit_evidence.neutral_ignored_predictions.count, 1);
  assert.equal(evidence.audit_evidence.score_explanation.scoreable_target_denominator, 1);
  assert.equal(evidence.audit_evidence.false_track_segment_count, 1);
  assert.ok(!evidence.audit_evidence.false_track_segments.some((segment) => segment.track_id === "neutral-track"));
  assert.equal((await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "accepted", note: "read-only token must not write" }),
  })).status, 401);
  const note = await (await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "accepted", note: "<script>untrusted note</script> Baseline evidence reviewed." }),
  })).json();
  assert.equal(note.verdict, "accepted");
  assert.equal(note.actorId, "operator/alice");
  assert.equal((await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "accepted", note: "x".repeat(10_000) }),
  })).status, 413);
  assert.equal((await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "forged", note: "not allowed" }),
  })).status, 422);
  const notes = await (await request(`/api/v1/operator/jobs/${created.id}/notes`, { headers })).json();
  assert.equal(notes.notes[0].note, "<script>untrusted note</script> Baseline evidence reviewed.");
  const publicResult = await jsonRequest(`/api/v1/submissions/${created.id}`);
  assert.equal(publicResult.result.scores.sample_counts.matches, 12);
  assert.equal(publicResult.result.diagnostics, undefined);
  assert.equal(publicResult.result.audit_evidence, undefined);
  assert.equal((await request("/api/v1/operator/jobs?cursor=bad", { headers })).status, 400);
});

test("any cross-scope credential collision fails closed while distinct scopes remain usable", async () => {
  const seeded = await (await submit(validBody(), "cross-scope-seed-0001")).json();
  const scopePairs = [
    ["submission", "runner"],
    ["submission", "operatorRead"],
    ["submission", "adjudicator"],
    ["runner", "operatorRead"],
    ["runner", "adjudicator"],
    ["operatorRead", "adjudicator"],
  ];
  for (const [left, right] of scopePairs) {
    const values = {
      submission: "submission-only-token",
      runner: "runner-only-token",
      operatorRead: "operator-read-only-token",
      adjudicator: "operator-write-only-token",
    };
    values[left] = "shared-cross-scope-token";
    values[right] = "shared-cross-scope-token";
    const collision = createApp({
      store,
      submissionKeys: values.submission,
      runnerToken: values.runner,
      operatorReadKeys: values.operatorRead,
      operatorAdjudicatorCredentials: { "operator/alice": values.adjudicator },
    });
    assert.equal((await requestFor(collision, "/api/v1/submissions", {
      method: "POST",
      headers: {
        authorization: "Bearer shared-cross-scope-token",
        "content-type": "application/json",
        "idempotency-key": `collision-${left}-${right}`,
      },
      body: JSON.stringify(validBody()),
    })).status, 401, `${left}/${right} submission scope`);
    assert.equal((await requestFor(collision, "/api/v1/internal/leases", {
      method: "POST",
      headers: { authorization: "Bearer shared-cross-scope-token" },
    })).status, 401, `${left}/${right} runner scope`);
    assert.equal((await requestFor(collision, "/api/v1/operator/jobs", {
      headers: { authorization: "Bearer shared-cross-scope-token" },
    })).status, 401, `${left}/${right} read scope`);
    assert.equal((await operatorNoteRequest(collision, seeded.id, "shared-cross-scope-token")).status, 401, `${left}/${right} write scope`);
  }

  const distinctStore = new MemoryStore();
  const distinct = createApp({
    store: distinctStore,
    submissionKeys: "submission-distinct-token",
    runnerToken: "runner-distinct-token",
    operatorReadKeys: "operator-read-distinct-token",
    operatorAdjudicatorCredentials: { "operator/alice": "operator-write-distinct-token" },
  });
  const distinctSubmission = await requestFor(distinct, "/api/v1/submissions", {
    method: "POST",
    headers: {
      authorization: "Bearer submission-distinct-token",
      "content-type": "application/json",
      "idempotency-key": "distinct-scopes-0001",
    },
    body: JSON.stringify(validBody()),
  });
  assert.equal(distinctSubmission.status, 201);
  const distinctJob = await distinctSubmission.json();
  assert.equal((await requestFor(distinct, "/api/v1/operator/jobs", {
    headers: { authorization: "Bearer operator-read-distinct-token" },
  })).status, 200);
  assert.equal((await requestFor(distinct, "/api/v1/internal/leases", {
    method: "POST",
    headers: { authorization: "Bearer runner-distinct-token" },
  })).status, 200);
  assert.equal((await operatorNoteRequest(distinct, distinctJob.id, "operator-write-distinct-token")).status, 201);
});

test("duplicate review aids are store-wide and never claim unavailable comparisons are clear", async () => {
  const firstCreate = await submit(validBody(), "duplicate-store-wide-01");
  assert.equal(firstCreate.status, 201);
  const first = await firstCreate.json();
  const secondCreate = await submit({ ...validBody(), name: "Safe baseline copy" }, "duplicate-store-wide-02");
  assert.equal(secondCreate.status, 201);
  const second = await secondCreate.json();
  const firstLeaseResponse = await lease();
  assert.equal(firstLeaseResponse.status, 200);
  const firstLease = await firstLeaseResponse.json();
  assert.ok([first.id, second.id].includes(firstLease.submission.id));
  const firstCallback = await result(firstLease.submission.id, { status: "succeeded", lease_token: firstLease.lease.token, report: scoredReport() });
  assert.equal(firstCallback.status, 200);
  const secondLeaseResponse = await lease();
  assert.equal(secondLeaseResponse.status, 200);
  const secondLease = await secondLeaseResponse.json();
  assert.ok([first.id, second.id].includes(secondLease.submission.id));
  assert.notEqual(secondLease.submission.id, firstLease.submission.id);
  const secondCallback = await result(secondLease.submission.id, { status: "succeeded", lease_token: secondLease.lease.token, report: scoredReport() });
  assert.equal(secondCallback.status, 200);
  assert.equal(store.rows.get(first.id).resultSha256, store.rows.get(second.id).resultSha256);

  const list = await (await request("/api/v1/operator/jobs?limit=100", { headers: { authorization: `Bearer ${OPERATOR_READ_TOKEN}` } })).json();
  assert.equal(list.comparison.scope, "store_wide");
  assert.equal(list.comparison.truncated, false);
  assert.equal(list.jobs.filter((job) => job.diagnostics.duplicate_model_fingerprint === "review").length, 2);
  assert.equal(list.jobs.filter((job) => job.diagnostics.duplicate_result_fingerprint === "review").length, 2);
});

test("adjudicator credentials map to distinct actors and cannot cross scopes", async () => {
  const created = await (await submit(validBody(), "multi-actor-0001")).json();
  const aliceNote = await (await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "needs_review", note: "Alice review." }),
  })).json();
  const bobNote = await (await request(`/api/v1/operator/jobs/${created.id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${OPERATOR_SECOND_WRITE_TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "accepted", note: "Bob adjudication." }),
  })).json();
  assert.equal(aliceNote.actorId, "operator/alice");
  assert.equal(bobNote.actorId, "operator/bob");
  assert.notEqual(aliceNote.actorId, bobNote.actorId);
  assert.equal((await request(`/api/v1/operator/jobs/${created.id}`, { headers: { authorization: `Bearer ${OPERATOR_WRITE_TOKEN}` } })).status, 401);
  assert.equal((await request(`/api/v1/operator/jobs/${created.id}`, { headers: { authorization: `Bearer ${OPERATOR_SECOND_WRITE_TOKEN}` } })).status, 401);
  const notes = await (await request(`/api/v1/operator/jobs/${created.id}/notes`, { headers: { authorization: `Bearer ${OPERATOR_READ_TOKEN}` } })).json();
  assert.deepEqual(notes.notes.map((note) => note.actorId).sort(), ["operator/alice", "operator/bob"].sort());

  const duplicateToken = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: {
      "operator/alice": OPERATOR_WRITE_TOKEN,
      "operator/bob": OPERATOR_WRITE_TOKEN,
    },
  });
  assert.equal((await operatorNoteRequest(duplicateToken, created.id, OPERATOR_WRITE_TOKEN)).status, 401);

  const duplicateActorArray = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: [
      { actorId: "operator/alice", token: OPERATOR_WRITE_TOKEN },
      { actorId: "operator/alice", token: OPERATOR_SECOND_WRITE_TOKEN },
    ],
  });
  assert.equal((await operatorNoteRequest(duplicateActorArray, created.id, OPERATOR_WRITE_TOKEN)).status, 401);

  const duplicateActorJson = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: '{"operator/alice":"alice-token-a","operator/alice":"alice-token-b"}',
  });
  assert.equal((await operatorNoteRequest(duplicateActorJson, created.id, "alice-token-a")).status, 401);

  const duplicateNormalizedActor = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: {
      "operator/alice": "alice-token-a",
      " operator/alice ": "alice-token-b",
    },
  });
  assert.equal((await operatorNoteRequest(duplicateNormalizedActor, created.id, "alice-token-a")).status, 401);

  for (const actorId of ["unattributed/foo", "legacy/operator"]) {
    const reservedActor = createApp({
      store,
      submissionKeys: SUBMISSION_KEY,
      runnerToken: RUNNER_TOKEN,
      operatorReadKeys: OPERATOR_READ_TOKEN,
      operatorAdjudicatorCredentials: { [actorId]: "reserved-token" },
    });
    assert.equal((await operatorNoteRequest(reservedActor, created.id, "reserved-token")).status, 401);
  }

  const unattributed = createApp({
    store,
    submissionKeys: SUBMISSION_KEY,
    runnerToken: RUNNER_TOKEN,
    operatorReadKeys: OPERATOR_READ_TOKEN,
    operatorAdjudicatorCredentials: { "unattributed-operator": OPERATOR_WRITE_TOKEN },
  });
  assert.equal((await operatorNoteRequest(unattributed, created.id, OPERATOR_WRITE_TOKEN)).status, 401);
});

test("Worker canonical audit hash verifies through API after parsing 1.0 as 1", async () => {
  const created = await (await submit(validBody(), "audit-hash-0001")).json();
  const leased = await (await lease()).json();
  const report = scoredReport();
  const rawReport = JSON.stringify(report).replace('"false_track_segment_count":1', '"false_track_segment_count":1.0');
  const callback = await request(`/api/v1/internal/submissions/${created.id}/result`, {
    method: "POST",
    headers: { authorization: `Bearer ${RUNNER_TOKEN}`, "content-type": "application/json" },
    body: `{"status":"succeeded","lease_token":${JSON.stringify(leased.lease.token)},"report":${rawReport}}`,
  });
  assert.equal(callback.status, 200);
  const evidence = await (await request(`/api/v1/operator/jobs/${created.id}/evidence`, { headers: { authorization: `Bearer ${OPERATOR_READ_TOKEN}` } })).json();
  assert.equal(evidence.audit_evidence.false_track_segment_count, 1);
  assert.equal(evidence.bounded_audit_evidence_sha256, await sha256(canonicalJson(evidence.audit_evidence)));
  assert.equal(evidence.bounded_audit_evidence_hash_algorithm, "sha256(cvbench.canonical-json/v1)");
});

test("hourly limits and payload limits are enforced", async () => {
  assert.equal((await submit(validBody(), "rate-limit-0001")).status, 201);
  assert.equal((await submit(validBody(), "rate-limit-0002")).status, 201);
  const limited = await submit(validBody(), "rate-limit-0003");
  assert.equal(limited.status, 429);
  assert.equal(limited.headers.get("retry-after"), "3600");

  const oversized = await submit({ ...validBody(), notes: "x".repeat(20_000) }, "oversized-0001");
  assert.equal(oversized.status, 413);
});

test("concurrent submissions cannot exceed the hourly limit", async () => {
  const responses = await Promise.all(
    Array.from({ length: 20 }, (_, index) => submit(validBody(), `concurrent-rate-${String(index).padStart(4, "0")}`)),
  );
  const statuses = responses.map((response) => response.status);
  assert.equal(statuses.filter((status) => status === 201).length, 2);
  assert.equal(statuses.filter((status) => status === 429).length, 18);
});

test("expired leases are requeued and stale callbacks are rejected", async () => {
  const created = await (await submit(validBody(), "lease-expiry-0001")).json();
  const first = await (await lease()).json();
  const row = store.rows.get(created.id);
  row.leaseExpiresAt = 1;
  const second = await (await lease()).json();
  assert.equal(second.submission.attempt, 2);
  assert.notEqual(second.lease.token, first.lease.token);
  assert.equal((await result(created.id, { status: "succeeded", lease_token: first.lease.token, report: scoredReport() })).status, 409);
});

test("an expired lease cannot complete before maintenance requeues it", async () => {
  const created = await (await submit(validBody(), "expired-callback-0001")).json();
  const leased = await (await lease()).json();
  store.rows.get(created.id).leaseExpiresAt = 1;
  const callback = await result(created.id, {
    status: "succeeded",
    lease_token: leased.lease.token,
    report: scoredReport(),
  });
  assert.equal(callback.status, 409);
  assert.equal((await jsonRequest(`/api/v1/submissions/${created.id}`)).status, "running");
});

test("the configured lease accepts a callback through the full 3000-second budget", async () => {
  const realDateNow = Date.now;
  const startedAt = realDateNow();
  try {
    Date.now = () => startedAt;
    const created = await (await submit(validBody(), "lease-budget-0001")).json();
    const leased = await (await lease()).json();
    assert.equal(Date.parse(leased.lease.expires_at) - Math.floor(startedAt / 1000) * 1000, 3_000_000);

    Date.now = () => Date.parse(leased.lease.expires_at);
    const callback = await result(created.id, {
      status: "succeeded",
      lease_token: leased.lease.token,
      report: scoredReport(),
    });
    assert.equal(callback.status, 200);
  } finally {
    Date.now = realDateNow;
  }
});

function validBody() {
  return {
    image: IMAGE,
    argv: ["python", "-m", "tracker"],
    name: "Safe baseline",
    model_version: "1",
    contact: "private@example.invalid",
  };
}

function scoredReport(runtimeIsolation = {}) {
  runtimeIsolation = {
    runtime: "docker",
    requested: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
    status: "verified",
    container_id: "a".repeat(64),
    mounts: [{ source: "/tmp/cvb-run", destination: "/run/cvbench" }],
    network_mode: "none",
    applied: { cpu_limit: 4, memory_limit_mb: 2048 },
    future_frame_isolation: true,
    ground_truth_access: false,
    repository_access: false,
    media_access: false,
    image_identity_verified: true,
    executed_container_user: "65532:65532",
    container_user_alignment_verified: true,
    expected_container_user: "65532:65532",
    socket_access: { owner_uid: 65532, owner_gid: 65532, directory_mode: "0o700", socket_mode: "0o600" },
    expected_mount: { source: "/tmp/cvb-run", destination: "/run/cvbench" },
    image_identity: {
      configured_reference: IMAGE,
      resolved_reference: IMAGE,
      resolved_image_id: `sha256:${"b".repeat(64)}`,
      executed_reference: IMAGE,
      executed_image_id: `sha256:${"b".repeat(64)}`,
    },
    ...runtimeIsolation,
  };
  return {
    schema_version: "cvbench.report/v1",
    run_id: "run-1",
    started_at: "2026-07-23T00:00:00Z",
    mode: "online_replay",
    benchmark: { id: PUBLIC_BENCHMARK.id, version: PUBLIC_BENCHMARK.version },
    system: { id: "safe", revision: "1", runtime: "docker", command: validBody().argv },
    outcome: {
      status: "completed",
      exit_code: 0,
      startup_time_ms: 100,
      time_to_first_output_ms: 200,
      errors: [],
      resolved_image: IMAGE,
      timed_out: false,
      crashed: false,
    },
    feed: {
      delivered_frames: 12,
      dropped_frames: 0,
      duplicate_frames: 0,
      black_frames: 0,
      feed_interruptions: 0,
      delayed_frames: 0,
      input_queue_depth: null,
      input_queue_depth_available: false,
    },
    metrics: {
      sample_counts: {
        ground_truth_records: 12,
        output_records: 12,
        matches: 12,
        continuity_matches: 12,
        neutral_ignored_predictions: 1,
      },
      coverage: { overall_observed: 1, overall_continuity: 1 },
      identity: { id_switches: 0 },
      acquisition: { total_eligible_targets: 1, rate: 1 },
      localization: { sample_count: 12, mean_iou: 0.91 },
      false_detections: { track_births: 1 },
      visible_dropouts: { count: 0 },
      reacquisition: { same_id_rate: 1 },
      latency: { sample_count: 12 },
      multi_target: { "1": { sample_count: 12 } },
      multi_object_tracking: { hota: 1, idf1: 1 },
      robustness: { occlusion_survival: 1 },
      long_running_stability: { passed: true },
    },
    runtime_isolation: runtimeIsolation,
    timing: {
      contract_version: "cvbench.timing-compute/v1",
      source: { immutable: true, frame_count: 12, duration_seconds: 10, sequences: [] },
      replay: { profile: "native", rate: 1, native_real_time: true, allowlisted: true },
      durations: {
        wall_seconds: 9,
        runner_total_seconds: 9.1,
        startup_seconds: 0.1,
        stream_delivery_seconds: 8,
        completion_seconds: 9,
        drain_seconds: 1,
        real_time_factor: 0.9,
        teardown_seconds: 0.1,
      },
      delivery: {
        policy_version: "cvbench.delivery-lossless/v1",
        replay_profile: "native",
        replay_rate: 1,
        effective_replay_rate: 1.25,
        delivered_frames_per_second: 1.5,
        deadline_missed_frames: 0,
        sender_pressure_frames: 0,
        frame_count: 12,
        delivered_frames: 12,
        transport_failed_frames: 0,
        policy_dropped_frames: 0,
        sender_blocking_time_ms: 0,
        benchmark_end_sender_call_ms: 0,
        delivery_backlog_ms: { sample_count: 12, minimum: 0, median: 0, p95: 0, maximum: 0 },
        sender_call_ms: { sample_count: 12, minimum: 0, median: 0, p95: 0, maximum: 0 },
        input_queue_depth: null,
        input_queue_depth_available: false,
        input_queue_depth_note: "not portable",
        semantics: "ordered progressive delivery",
        per_frame: [],
      },
      processing_latency_ms: { sample_count: 12, minimum: 1, median: 2, p95: 3, maximum: 4 },
      native_source_offset_ms: { sample_count: 12, minimum: 1, median: 2, p95: 3, maximum: 4 },
      output: {
        records: 12,
        records_per_native_source_second: 1.2,
        records_per_completion_second: 1.33,
        late_after_benchmark_end: 0,
        late_output_policy: "outputs after benchmark_end remain causal but count toward bounded drain",
      },
      clocks: {
        source: "immutable scenario-relative source timestamps",
        delivery: "independent monotonic source clock",
        completion: "benchmark-end delivery through scoring deadline",
      },
    },
    resources: {
      sample_count: 2,
      runtime_seconds: 9,
      cpu_time_seconds: 15,
      cpu_seconds_per_native_source_second: 1.5,
      average_cpu_percent: 150,
      peak_cpu_percent: 200,
      average_ram_bytes: 512,
      peak_ram_bytes: 1024,
      gpu_available: false,
      peak_vram_bytes: null,
      gpu_accounting: { available: false, isolated: false, authoritative: false, note: "not assigned" },
      disk_read_bytes: 100,
      disk_write_bytes: 200,
      network_rx_bytes: 0,
      network_tx_bytes: 0,
      peak_process_count: 2,
      peak_thread_count: 4,
      memory_growth_bytes: 0,
      by_scenario: {},
      by_target_count: {},
      by_phase: {},
      cpu_time_by_phase_seconds: {},
      fault_injection_samples: 0,
      accounting_scope: "container_cgroup_v2_external",
      authoritative: true,
      over_time: [
        {
          elapsed_ms: 1000,
          cpu_percent: 100,
          cpu_time_seconds: 5,
          memory_bytes: 0,
          memory_limit_bytes: 2147483648,
          memory_peak_bytes: 512,
          gpu_percent: null,
          vram_bytes: null,
          disk_read_bytes: 50,
          disk_write_bytes: 100,
          network_rx_bytes: null,
          network_tx_bytes: null,
          process_count: 1,
          thread_count: null,
          phase: "delivery",
          scenario: "public",
          target_count: 1,
          fault_injection: false,
          accounting_source: "host_cgroup_v2",
        },
        {
          elapsed_ms: 9000,
          cpu_percent: 200,
          cpu_time_seconds: 15,
          memory_bytes: 1024,
          memory_limit_bytes: 2147483648,
          memory_peak_bytes: 1024,
          gpu_percent: null,
          vram_bytes: null,
          disk_read_bytes: 100,
          disk_write_bytes: 200,
          network_rx_bytes: null,
          network_tx_bytes: null,
          process_count: 2,
          thread_count: null,
          phase: "drain",
          scenario: null,
          target_count: null,
          fault_injection: false,
          accounting_source: "host_cgroup_v2",
          final_cumulative: true,
        },
      ],
      accounting_availability: {
        external_cgroup_v2: true,
        final_cumulative_cpu_sample: true,
        cpu_time: true,
        cpu_percent: true,
        peak_ram: true,
        disk_io: true,
      },
    },
    leaderboard: {
      policy_version: "cvbench.pareto/v1",
      ranking_method: "pareto",
      composite_score: null,
      replay_class: "native",
      class_id: "native/cpu-2/realtime",
      compute_tier: "cpu-2",
      completion_tier: "realtime",
      eligible: true,
      disqualifications: [],
      raw_axes: {
        accuracy: { acquisition_rate: 1, observed_coverage: 1, mean_iou: 0.91, hota: 1, idf1: 1 },
        efficiency: {
          cpu_seconds_per_native_source_second: 1.5,
          real_time_factor: 0.9,
          peak_ram_bytes: 1024,
          disk_read_bytes: 100,
          disk_write_bytes: 200,
        },
      },
      comparison_rule: "Compare only identical fingerprints and class IDs by Pareto dominance.",
    },
    findings: [],
    comparison: [],
    audit_evidence: {
      schema_version: "cvbench.audit/v1",
      review_disposition: "review_aid_only; never an automatic disqualification",
      frame_samples: [
        {
          matches: [{ target_id: "target-1", iou: 0.91 }],
          ground_truth: [{ count_reason: "matched_observed_and_counted" }],
          predictions: [{ track_id: "track-1" }, { track_id: "neutral-track", neutral_ignored: true }],
        },
      ],
      sampled_frame_count: 1,
      source_frame_count: 12,
      neutral_ignored_predictions: { count: 1, annotation_ids: ["ignore-1"] },
      false_track_segment_count: 1,
      score_explanation: {
        scoreable_target_denominator: 1,
        coverage_denominators: { observed_coverage: 1, eligible_targets: 1 },
        component_counts: { localization: 12 },
      },
      flags: [{ id: "false_track", status: "flagged", review_aid_only: true }],
      false_track_segments: [{ track_id: "false-track", duration_ms: 100 }],
      resources_and_isolation: { runtime_isolation: runtimeIsolation },
      occlusion_and_reacquisition: { occlusion_rows: 0 },
      timeline: { external_clock: "collector_received_timestamp_ns" },
      timing_compute: { contract_version: "cvbench.timing-compute/v1" },
      reproducibility: { raw_evidence_available: false },
      serialized_byte_budget: { max_bytes: 262144, truncated: false },
      budget_omitted: {
        frame_samples: 0,
        other_items: 0,
        records_in_omitted_frames: { ground_truth: 0, matches: 0, predictions: 0 },
      },
    },
    provenance: {
      raw_evidence_available: false,
      timing_compute_contract: "cvbench.timing-compute/v1",
      delivery_policy: "cvbench.delivery-lossless/v1",
      replay_profile: "native",
      replay_rate: 1,
      leaderboard_class: "native/cpu-2/realtime",
      comparison_fingerprint: "a".repeat(64),
      benchmark_path: "benchmarks/public-whole-system-v2.yaml",
      benchmark_sha256: "b".repeat(64),
      system_path: "systems/submitted.yaml",
      system_sha256: "c".repeat(64),
      scenario_manifests: ["scenarios/public.yaml"],
      resolved_container_image: IMAGE,
      resolved_container_image_id: `sha256:${"b".repeat(64)}`,
      executed_container_image_id: `sha256:${"b".repeat(64)}`,
      command: "cvbench run",
      system_command: validBody().argv,
      matching: {
        algorithm: "deterministic Hungarian assignment",
        minimum_iou: 0.5,
        maximum_center_error_px: 50,
        class_agnostic: false,
        ignore_match_iou: 0.5,
      },
      external_clock: "time.monotonic_ns",
      comparison_inputs: {
        benchmark_id: PUBLIC_BENCHMARK.id,
        benchmark_version: PUBLIC_BENCHMARK.version,
        input_mode: "online_replay",
        timing_compute_contract: "cvbench.timing-compute/v1",
        delivery_policy: "cvbench.delivery-lossless/v1",
        replay_profile: "native",
        playback_rate: 1,
        resource_envelope: {
          benchmark: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
          system: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
        },
        run_budgets: {
          max_run_seconds: 20,
          max_drain_seconds: 3,
          max_output_records: 10000,
          max_output_line_bytes: 1048576,
          max_total_output_bytes: 16777216,
          max_output_records_per_second: 1000,
        },
        accounting_availability: {
          external_cgroup_v2: true,
          final_cumulative_cpu_sample: true,
          cpu_time: true,
          cpu_percent: true,
          peak_ram: true,
          disk_io: true,
        },
        thresholds: { minimum_match_iou: 0.5 },
        evaluation_order: { mode: "configured_seed", algorithm: "sha256", seed: "public-v2" },
        scenarios: [{ id: "public" }],
      },
      resource_envelope: {
        benchmark: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
        system: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
      },
      run_budgets: {
        max_run_seconds: 20,
        max_drain_seconds: 3,
        max_output_records: 10000,
        max_output_line_bytes: 1048576,
        max_total_output_bytes: 16777216,
        max_output_records_per_second: 1000,
      },
      accounting_availability: {
        external_cgroup_v2: true,
        final_cumulative_cpu_sample: true,
        cpu_time: true,
        cpu_percent: true,
        peak_ram: true,
        disk_io: true,
      },
      evaluation_order: {
        scenario_ids: PUBLIC_BENCHMARK.scenario_ids,
        mode: "configured_seed",
        algorithm: "sha256",
        seed: "public-v2",
        private_per_run: false,
        run_scoped_sequence_ids: true,
        public_calibration_note: "Public scenarios are recognizable.",
      },
      platform: { os: "posix" },
      bounded_audit_evidence_sha256: null,
      bounded_audit_evidence_hash_algorithm: "sha256(cvbench.canonical-json/v1)",
    },
    diagnostics: {
      collector_errors: [],
      sut_stderr: ["<script>throw new Error('untrusted')</script>"],
      match_count: 12,
    },
    limitations: [],
    runner: {
      schema_version: "cvbench.runner/v1",
      commit: "d".repeat(40),
      workflow_run_url: "https://github.com/example/cvbench/actions/runs/1",
      workflow_name: "Run public CVBench submission",
    },
  };
}

function request(path, options) {
  return requestFor(app, path, options);
}

function requestFor(appInstance, path, options) {
  return appInstance.fetch(new Request(`https://cvbench.test${path}`, options));
}

async function jsonRequest(path, options) {
  return (await request(path, options)).json();
}

function submit(body, idempotencyKey, token = SUBMISSION_KEY) {
  return request("/api/v1/submissions", {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "idempotency-key": idempotencyKey,
    },
    body: JSON.stringify(body),
  });
}

function lease(token = RUNNER_TOKEN) {
  return request("/api/v1/internal/leases", { method: "POST", headers: { authorization: `Bearer ${token}` } });
}

function result(id, body, token = RUNNER_TOKEN) {
  return request(`/api/v1/internal/submissions/${id}/result`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function operatorNoteRequest(appInstance, id, token) {
  return appInstance.fetch(new Request(`https://cvbench.test/api/v1/operator/jobs/${id}/notes`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ verdict: "accepted", note: "must fail closed" }),
  }));
}

async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
