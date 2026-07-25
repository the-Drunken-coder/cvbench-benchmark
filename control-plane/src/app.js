import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import REPORT_SCHEMA from "../../schemas/report-v1.schema.json" with { type: "json" };
import TIMING_COMPUTE_SCHEMA from "../../schemas/timing-compute-v1.schema.json" with { type: "json" };

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const MAX_SUBMISSION_BYTES = 16 * 1024;
const MAX_RESULT_BYTES = 1024 * 1024;
const MAX_OPERATOR_NOTE_BYTES = 8 * 1024;
const VALID_JOB_STATUSES = new Set(["queued", "running", "succeeded", "failed"]);
const IMAGE_PATTERN = /^(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?\/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[a-f0-9]{64}$/;
const ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const REPORT_SCHEMA_AJV = new Ajv2020({ allErrors: true, strict: true, allowUnionTypes: true });
addFormats(REPORT_SCHEMA_AJV);
REPORT_SCHEMA_AJV.addSchema(TIMING_COMPUTE_SCHEMA);
const validateReportSchema = REPORT_SCHEMA_AJV.compile(REPORT_SCHEMA);
const OPENAPI_REPORT_SCHEMA = JSON.parse(JSON.stringify(REPORT_SCHEMA));
const OPENAPI_TIMING_COMPUTE_SCHEMA = JSON.parse(JSON.stringify(TIMING_COMPUTE_SCHEMA));
OPENAPI_REPORT_SCHEMA.properties.timing = { $ref: "#/components/schemas/TimingComputeV1" };

export const PUBLIC_BENCHMARK = Object.freeze({
  id: "public-whole-system-tracking",
  version: "2.0.0",
  manifest: "benchmarks/public-whole-system-v2.yaml",
  timing_compute_contract: "cvbench.timing-compute/v1",
  delivery_policy: "cvbench.delivery-lossless/v1",
  replay_profile: "native",
  replay_rate: 1,
  leaderboard_policy: "cvbench.pareto/v1",
  scenario_count: 16,
  scenario_ids: Object.freeze([
    "synthetic-acquisition",
    "synthetic-visible-retention",
    "synthetic-occlusion-reacquisition",
    "synthetic-multi-target-pair",
    "synthetic-multi-target-identity",
    "synthetic-false-detection",
    "synthetic-resource-stress",
    "synthetic-occlusion-gap-100ms",
    "synthetic-occlusion-gap-250ms",
    "synthetic-occlusion-gap-500ms",
    "synthetic-occlusion-gap-1000ms",
    "synthetic-occlusion-gap-2000ms",
    "synthetic-track-id-churn",
    "rvmot-a1c9",
    "rvmot-b7e2",
    "rvmot-c4f6",
  ]),
});

export function createApp(options) {
  const submissionKeys = splitKeys(options.submissionKeys);
  const runnerToken = String(options.runnerToken || "");
  const operatorReadKeys = splitKeys(options.operatorReadKeys || options.operatorToken);
  const operatorAdjudicatorCredentials = parseAdjudicatorCredentials(options.operatorAdjudicatorCredentials);
  const credentialScopesAreValid = uniqueCredentialScopes({
    submission: submissionKeys,
    runner: [runnerToken],
    operatorRead: operatorReadKeys,
    adjudicator: operatorAdjudicatorCredentials.map(({ token }) => token),
  });
  const config = {
    store: options.store,
    assets: options.assets,
    submissionKeys: credentialScopesAreValid ? submissionKeys : [],
    runnerToken: credentialScopesAreValid ? runnerToken : "",
    operatorReadKeys: credentialScopesAreValid ? operatorReadKeys : [],
    operatorAdjudicatorCredentials: credentialScopesAreValid ? operatorAdjudicatorCredentials : [],
    maxSubmissionsPerHour: boundedInteger(options.maxSubmissionsPerHour, 20, 1, 1000),
    leaseSeconds: boundedInteger(options.leaseSeconds, 3000, 60, 7200),
  };

  return {
    async fetch(request) {
      try {
        return await route(request, config);
      } catch (error) {
        console.error("unhandled request error", error);
        return problem(500, "internal_error", "The control plane could not process the request.");
      }
    },
  };
}

async function route(request, config) {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/api/v1/")) return serveAsset(request, config.assets);

  if (request.method === "GET" && url.pathname === "/api/v1/health") {
    try {
      await config.store.health();
      return json({ status: "ok", service: "cvbench-control-plane", version: "v1" });
    } catch {
      return problem(503, "database_unavailable", "The queue database is unavailable.");
    }
  }
  if (request.method === "GET" && url.pathname === "/api/v1/contract") return json(CONTRACT);
  if (request.method === "GET" && url.pathname === "/api/v1/openapi.json") return json(OPENAPI);

  if (request.method === "POST" && url.pathname === "/api/v1/submissions") {
    const token = bearerToken(request);
    if (!(await authorized(token, config.submissionKeys))) return unauthorized("submission API key");
    const parsed = await readJson(request, MAX_SUBMISSION_BYTES);
    if (parsed.error) return parsed.error;
    const validation = validateSubmission(parsed.value);
    if (validation.error) return problem(422, "invalid_submission", validation.error);
    const idempotencyKey = request.headers.get("idempotency-key") || "";
    if (!/^[A-Za-z0-9._:-]{8,128}$/.test(idempotencyKey)) {
      return problem(400, "invalid_idempotency_key", "Idempotency-Key must be 8-128 safe ASCII characters.");
    }

    const now = unixTime();
    const requestHash = await sha256(stableJson(validation.value));
    const submitterKeyHash = await sha256(token);
    const result = await config.store.createSubmission(
      {
        id: crypto.randomUUID(),
        ...validation.value,
        idempotencyKey,
        requestHash,
        submitterKeyHash,
        now,
      },
      config.maxSubmissionsPerHour,
    );
    if (result.kind === "conflict") {
      return problem(409, "idempotency_conflict", "This Idempotency-Key was already used for a different request.");
    }
    if (result.kind === "rate_limited") {
      return problem(429, "rate_limited", "This API key has reached its hourly submission limit.", { "retry-after": "3600" });
    }
    return json(publicSubmission(result.submission), result.kind === "created" ? 201 : 200, {
      location: `/api/v1/submissions/${result.submission.id}`,
      "idempotency-replayed": result.kind === "replay" ? "true" : "false",
    });
  }

  const submissionMatch = url.pathname.match(/^\/api\/v1\/submissions\/([^/]+)$/);
  if (request.method === "GET" && submissionMatch) {
    const id = submissionMatch[1];
    if (!ID_PATTERN.test(id)) return problem(404, "not_found", "Submission not found.");
    const submission = await config.store.getSubmission(id);
    return submission ? json(publicSubmission(submission)) : problem(404, "not_found", "Submission not found.");
  }

  if (request.method === "POST" && url.pathname === "/api/v1/internal/leases") {
    if (!(await authorized(bearerToken(request), [config.runnerToken]))) return unauthorized("runner token");
    const leaseToken = randomToken();
    const now = unixTime();
    const submission = await config.store.leaseJob({
      now,
      leaseExpiresAt: now + config.leaseSeconds,
      leaseTokenHash: await sha256(leaseToken),
    });
    if (!submission) return new Response(null, { status: 204 });
    return json({
      submission: runnerSubmission(submission),
      lease: {
        token: leaseToken,
        expires_at: iso(submission.leaseExpiresAt),
        result_url: `${url.origin}/api/v1/internal/submissions/${submission.id}/result`,
        max_result_bytes: MAX_RESULT_BYTES,
      },
    });
  }

  const resultMatch = url.pathname.match(/^\/api\/v1\/internal\/submissions\/([^/]+)\/result$/);
  if (request.method === "POST" && resultMatch) {
    if (!(await authorized(bearerToken(request), [config.runnerToken]))) return unauthorized("runner token");
    if (!ID_PATTERN.test(resultMatch[1])) return problem(404, "not_found", "Submission not found.");
    const parsed = await readJson(request, MAX_RESULT_BYTES);
    if (parsed.error) return parsed.error;
    const validation = validateResult(parsed.value);
    if (validation.error) return problem(422, "invalid_result", validation.error);
    if (validation.value.report && !matchesPublicBenchmark(validation.value.report.benchmark)) {
      return problem(422, "invalid_result", "The report benchmark does not match the assigned public suite.");
    }
    if (validation.value.report && !matchesPublicTimingContract(validation.value.report)) {
      return problem(
        422,
        "invalid_result",
        "The report timing/compute contract does not match the native public leaderboard class.",
      );
    }
    const submission = await config.store.getSubmission(resultMatch[1]);
    if (!submission) return problem(404, "not_found", "Submission not found.");
    if (validation.value.report && !reportMatchesSubmission(validation.value.report, submission)) {
      return problem(422, "invalid_result", "The report image or command does not match the leased submission.");
    }
    const report = validation.value.report ? await authoritativeReport(validation.value.report) : null;
    const completed = await config.store.completeJob({
      id: resultMatch[1],
      leaseTokenHash: await sha256(validation.value.leaseToken),
      status: validation.value.status,
      report,
      resultSha256: report ? await sha256(canonicalJson(report)) : null,
      error: validation.value.error,
      now: unixTime(),
    });
    return completed
      ? json(publicSubmission(completed))
      : problem(409, "invalid_transition", "The job is not running or the lease token is invalid or stale.");
  }

  if (request.method === "POST" && url.pathname === "/api/v1/internal/maintenance") {
    if (!(await authorized(bearerToken(request), [config.runnerToken]))) return unauthorized("runner token");
    const requeued = await config.store.requeueExpired(unixTime());
    return json({ requeued });
  }

  const operatorMatch = url.pathname.match(/^\/api\/v1\/operator\/jobs(?:\/([^/]+)(?:\/(audit|evidence|notes))?)?$/);
  if (operatorMatch) {
    const id = operatorMatch[1];
    const subresource = operatorMatch[2];
    const write = request.method === "POST" && subresource === "notes";
    const token = bearerToken(request);
    const actorId = write ? await actorForCredential(token, config.operatorAdjudicatorCredentials) : null;
    if (write ? !actorId : !(await authorized(token, config.operatorReadKeys))) {
      return unauthorized(write ? "adjudicator credential" : "operator read token");
    }
    if (!id) {
      if (request.method !== "GET") return problem(405, "method_not_allowed", "Only GET is supported for the operator job list.");
      const status = url.searchParams.get("status") || "";
      const model = url.searchParams.get("model") || "";
      const limit = boundedInteger(url.searchParams.get("limit"), 25, 1, 100);
      if (status && !VALID_JOB_STATUSES.has(status)) return problem(400, "invalid_status", "status must be queued, running, succeeded, or failed.");
      const cursor = parseCursor(url.searchParams.get("cursor"));
      if (url.searchParams.has("cursor") && !cursor) return problem(400, "invalid_cursor", "cursor must be a timestamp and UUID returned by the operator list.");
      const page = await config.store.listSubmissions({ status, model, limit, cursor });
      const comparisons = await config.store.operatorComparisons();
      return json({ schema_version: "cvbench.operator/v1", jobs: page.rows.map((row) => operatorSummary(row, comparisons)), next_cursor: encodeCursor(page.nextCursor), comparison: comparisonSummary(comparisons) });
    }
    if (!ID_PATTERN.test(id)) return problem(404, "not_found", "Job not found.");
    const row = await config.store.getSubmission(id);
    if (!row) return problem(404, "not_found", "Job not found.");
    if (request.method === "GET" && !subresource) return json(await operatorDetail(row, config.store));
    if (request.method === "GET" && subresource === "audit") return json(await operatorAudit(row, config.store));
    if (request.method === "GET" && subresource === "evidence") return json(operatorEvidence(row));
    if (request.method === "GET" && subresource === "notes") return json({ schema_version: "cvbench.operator/v1", notes: await config.store.listOperatorNotes(id) });
    if (request.method === "POST" && subresource === "notes") {
      const parsed = await readJson(request, MAX_OPERATOR_NOTE_BYTES);
      if (parsed.error) return parsed.error;
      const validation = validateOperatorNote(parsed.value);
      if (validation.error) return problem(422, "invalid_operator_note", validation.error);
      const note = await config.store.addOperatorNote({
        id: crypto.randomUUID(),
        submissionId: id,
        verdict: validation.value.verdict,
        note: validation.value.note,
        createdAt: unixTime(),
        operatorKeyHash: await sha256(token),
        actorId,
      });
      return json(note, 201);
    }
    return problem(405, "method_not_allowed", "Unsupported operator job operation.");
  }

  return problem(404, "not_found", "API route not found.");
}

async function serveAsset(request, assets) {
  if (!assets || !["GET", "HEAD"].includes(request.method)) return problem(404, "not_found", "Not found.");
  const response = await assets.fetch(request);
  const kind = catalogAssetKind(new URL(request.url).pathname);
  const contentType = response.headers.get("content-type") || "";
  const poisonedSuccess = response.status === 200 && (kind === "json" ? !contentType.includes("json") : !contentType.startsWith("image/jpeg"));
  if (kind && (response.status === 404 || poisonedSuccess)) {
    const missing = kind === "json"
      ? problem(404, "not_found", "Catalog resource not found.")
      : new Response(null, { status: 404, headers: { "cache-control": "no-store", "content-type": "image/jpeg" } });
    return secureAssetResponse(missing);
  }
  return secureAssetResponse(response);
}

function catalogAssetKind(pathname) {
  if (pathname === "/.well-known/cvbench-scenarios.json" || /^\/scenario-catalog\/v1\/[^?#]*\.json$/.test(pathname)) {
    return "json";
  }
  if (/^\/scenario-catalog\/v1\/assets\/sha256\/[^/]+\.jpg$/.test(pathname)) return "jpeg";
  return null;
}

function secureAssetResponse(response) {
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff");
  headers.set("referrer-policy", "strict-origin-when-cross-origin");
  headers.set("content-security-policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function validateSubmission(value) {
  if (!isObject(value)) return { error: "Request body must be a JSON object." };
  if (unknownKeys(value, ["image", "argv", "name", "model_version", "contact", "notes"]).length) {
    return { error: `Unknown fields: ${unknownKeys(value, ["image", "argv", "name", "model_version", "contact", "notes"]).join(", ")}.` };
  }
  if (typeof value.image !== "string" || !IMAGE_PATTERN.test(value.image)) {
    return { error: "image must be a lowercase OCI reference pinned with @sha256:<64 lowercase hex characters>." };
  }
  if (!Array.isArray(value.argv) || value.argv.length < 1 || value.argv.length > 32) {
    return { error: "argv must be an array containing 1-32 arguments." };
  }
  for (const argument of value.argv) {
    if (typeof argument !== "string" || argument.length < 1 || argument.length > 256 || /[\0-\x1f\x7f]/.test(argument)) {
      return { error: "Each argv item must be a 1-256 character string without control characters." };
    }
  }
  const name = cleanText(value.name, "name", 1, 100);
  if (name.error) return name;
  const modelVersion = cleanText(value.model_version, "model_version", 1, 100);
  if (modelVersion.error) return modelVersion;
  const contact = optionalText(value.contact, "contact", 200);
  if (contact.error) return contact;
  const notes = optionalText(value.notes, "notes", 1000);
  if (notes.error) return notes;
  return {
    value: {
      image: value.image,
      argv: [...value.argv],
      name: name.value,
      modelVersion: modelVersion.value,
      contact: contact.value,
      notes: notes.value,
    },
  };
}

function validateResult(value) {
  if (!isObject(value)) return { error: "Request body must be a JSON object." };
  if (unknownKeys(value, ["status", "lease_token", "report", "error"]).length) return { error: "Result contains unknown fields." };
  if (!['succeeded', 'failed'].includes(value.status)) return { error: "status must be succeeded or failed." };
  if (typeof value.lease_token !== "string" || value.lease_token.length < 32 || value.lease_token.length > 200) {
    return { error: "lease_token is invalid." };
  }
  if (value.status === "succeeded" && !isObject(value.report)) return { error: "A succeeded result requires a report object." };
  if (value.status === "succeeded" && "error" in value) return { error: "A succeeded result cannot include error." };
  if (value.status === "failed" && "report" in value) return { error: "A failed result cannot include report." };
  if (value.status === "succeeded") {
    const reportError = validateSuccessfulReport(value.report);
    if (reportError) return { error: reportError };
  }
  if (value.status === "failed" && (typeof value.error !== "string" || value.error.length < 1 || value.error.length > 2000)) {
    return { error: "A failed result requires an error string of at most 2000 characters." };
  }
  return {
    value: {
      status: value.status,
      leaseToken: value.lease_token,
      report: value.status === "succeeded" ? value.report : null,
      error: value.status === "failed" ? value.error : null,
    },
  };
}

function finiteNumber(value, { positive = false } = {}) {
  return typeof value === "number" && Number.isFinite(value) && (positive ? value > 0 : value >= 0);
}

function sameNumber(left, right) {
  if (!finiteNumber(left) || !finiteNumber(right)) return false;
  return Math.abs(left - right) <= Math.max(1e-9, Math.abs(left), Math.abs(right)) * 1e-9;
}

function validateAuthoritativeResources(resources, sourceDurationSeconds) {
  const samples = resources.over_time;
  if (!Array.isArray(samples) || samples.length < 1 || resources.sample_count !== samples.length) {
    return false;
  }
  if (samples.some((sample) => sample.accounting_source !== "host_cgroup_v2")) return false;
  const finals = samples.filter((sample) => sample.final_cumulative === true);
  if (finals.length !== 1 || finals[0] !== samples.at(-1)) return false;
  const present = (field) => samples.map((sample) => sample[field]).filter(finiteNumber);
  const cpuPercent = present("cpu_percent");
  const memory = present("memory_bytes");
  const peakMemory = [...memory, ...present("memory_peak_bytes")];
  const diskRead = present("disk_read_bytes");
  const diskWrite = present("disk_write_bytes");
  const final = finals[0];
  return (
    cpuPercent.length > 0
    && memory.length > 0
    && diskRead.length > 0
    && diskWrite.length > 0
    && sameNumber(resources.cpu_time_seconds, final.cpu_time_seconds)
    && sameNumber(
      resources.cpu_seconds_per_native_source_second,
      final.cpu_time_seconds / sourceDurationSeconds,
    )
    && sameNumber(resources.average_cpu_percent, cpuPercent.reduce((sum, value) => sum + value, 0) / cpuPercent.length)
    && sameNumber(resources.peak_cpu_percent, Math.max(...cpuPercent))
    && sameNumber(resources.average_ram_bytes, memory.reduce((sum, value) => sum + value, 0) / memory.length)
    && sameNumber(resources.peak_ram_bytes, Math.max(...peakMemory))
    && sameNumber(resources.disk_read_bytes, Math.max(...diskRead))
    && sameNumber(resources.disk_write_bytes, Math.max(...diskWrite))
  );
}

function validateSuccessfulReport(report) {
  if (!validateReportSchema(report)) {
    const first = validateReportSchema.errors?.[0];
    const location = first?.instancePath || "<report>";
    return `report violates cvbench.report/v1 at ${location}: ${first?.message || "invalid report"}.`;
  }
  if (!matchesPublicBenchmark(report.benchmark)) return "Report benchmark does not match the assigned public suite.";
  if (
    report.outcome.status !== "completed"
    || report.outcome.exit_code !== 0
    || report.outcome.timed_out !== false
    || report.outcome.crashed !== false
  ) return "A succeeded callback requires a completed report outcome.";
  const isolation = report.runtime_isolation;
  if (
    isolation.runtime !== "docker"
    || isolation.status !== "verified"
    || isolation.network_mode !== "none"
    || isolation.future_frame_isolation !== true
    || isolation.ground_truth_access !== false
    || isolation.repository_access !== false
    || isolation.media_access !== false
    || isolation.image_identity_verified !== true
    || isolation.container_user_alignment_verified !== true
  ) return "A succeeded callback requires fully verified Docker isolation.";

  const timing = report.timing;
  if (
    timing.contract_version !== PUBLIC_BENCHMARK.timing_compute_contract
    || timing.source.immutable !== true
    || timing.replay.profile !== PUBLIC_BENCHMARK.replay_profile
    || timing.replay.rate !== PUBLIC_BENCHMARK.replay_rate
    || timing.replay.native_real_time !== true
    || timing.replay.allowlisted !== true
    || timing.delivery.policy_version !== PUBLIC_BENCHMARK.delivery_policy
  ) return "report.timing is incomplete or violates the assigned timing contract.";

  const resources = report.resources;
  const accounting = resources?.accounting_availability;
  const resourceAxes = [
    "cpu_time_seconds", "cpu_seconds_per_native_source_second", "average_cpu_percent",
    "peak_cpu_percent", "peak_ram_bytes", "disk_read_bytes", "disk_write_bytes",
  ];
  const accountingAxes = [
    "external_cgroup_v2", "final_cumulative_cpu_sample", "cpu_time", "cpu_percent",
    "peak_ram", "disk_io",
  ];
  if (
    resources.accounting_scope !== "container_cgroup_v2_external"
    || resources.authoritative !== true
    || resourceAxes.some((key) => !finiteNumber(resources[key]))
    || accountingAxes.some((key) => accounting[key] !== true)
    || !validateAuthoritativeResources(resources, timing.source.duration_seconds)
  ) return "report.resources lacks mandatory authoritative external cgroup axes.";

  const leaderboard = report.leaderboard;
  if (
    leaderboard.policy_version !== PUBLIC_BENCHMARK.leaderboard_policy
    || leaderboard.replay_class !== PUBLIC_BENCHMARK.replay_profile
    || leaderboard.eligible !== true
    || typeof leaderboard.class_id !== "string"
    || leaderboard.class_id.length < 1
    || leaderboard.disqualifications.length !== 0
  ) return "report.leaderboard must be eligible with a non-null class and retained raw axes.";

  const provenance = report.provenance;
  if (
    !/^[0-9a-f]{64}$/.test(provenance.comparison_fingerprint || "")
    || provenance.leaderboard_class !== leaderboard.class_id
    || provenance.resource_envelope.system?.cpu_limit !== 4
    || provenance.resource_envelope.system?.memory_limit_mb !== 2048
    || provenance.resource_envelope.system?.network_access !== false
    || !finiteNumber(provenance.run_budgets.max_run_seconds, { positive: true })
    || !finiteNumber(provenance.run_budgets.max_drain_seconds)
    || !Number.isInteger(provenance.run_budgets.max_output_records)
    || canonicalJson(provenance.accounting_availability) !== canonicalJson(accounting)
  ) return "report provenance, metrics, or audit evidence is incomplete.";
  if (
    report.runner?.schema_version !== "cvbench.runner/v1"
    || !Object.hasOwn(report.runner, "commit")
    || !Object.hasOwn(report.runner, "workflow_run_url")
    || !Object.hasOwn(report.runner, "workflow_name")
  ) return "report.runner must contain complete trusted-runner metadata.";
  return null;
}

function reportMatchesSubmission(report, submission) {
  const image = submission.image;
  const imageIdentity = report.runtime_isolation.image_identity;
  const imageReferences = [
    report.outcome.resolved_image,
    report.provenance.resolved_container_image,
    imageIdentity.configured_reference,
    imageIdentity.resolved_reference,
    imageIdentity.executed_reference,
  ];
  return (
    imageReferences.every((value) => value === image)
    && canonicalJson(report.system.command) === canonicalJson(submission.argv)
    && canonicalJson(report.provenance.system_command) === canonicalJson(submission.argv)
  );
}

async function authoritativeReport(report) {
  const auditEvidence = isObject(report.audit_evidence) ? report.audit_evidence : null;
  return {
    ...report,
    provenance: {
      ...(isObject(report.provenance) ? report.provenance : {}),
      raw_evidence_available: false,
      bounded_audit_evidence_sha256: auditEvidence ? await sha256(canonicalJson(auditEvidence)) : null,
      bounded_audit_evidence_hash_algorithm: "sha256(cvbench.canonical-json/v1)",
    },
  };
}

function publicSubmission(value) {
  return {
    id: value.id,
    status: value.status,
    model: { name: value.name, version: value.modelVersion, image: value.image, argv: value.argv },
    benchmark: PUBLIC_BENCHMARK,
    attempt: value.attempt,
    result: publicResultSummary(value.result),
    error: value.error,
    created_at: iso(value.createdAt),
    updated_at: iso(value.updatedAt),
    started_at: iso(value.startedAt),
    completed_at: iso(value.completedAt),
  };
}

function operatorSummary(value, comparisons = null) {
  const report = value.result;
  const provenance = report?.provenance || {};
  const runner = report?.runner || report?.control_plane?.runner || {};
  const audit = report?.audit_evidence;
  const duplicateModel = duplicateStatus(comparisons, "duplicateImages", value.image);
  const duplicateResult = duplicateStatus(comparisons, "duplicateResults", value.resultSha256);
  return {
    id: value.id,
    status: value.status,
    model: { name: value.name, version: value.modelVersion, image: value.image, argv: value.argv },
    queue: {
      attempt: value.attempt,
      retries: Math.max(0, value.attempt - 1),
      created_at: iso(value.createdAt),
      updated_at: iso(value.updatedAt),
      started_at: iso(value.startedAt),
      completed_at: iso(value.completedAt),
      lease_expires_at: iso(value.leaseExpiresAt),
    },
    provenance: {
      benchmark: report?.benchmark || PUBLIC_BENCHMARK,
      scenario_manifests: provenance.scenario_manifests || [],
      comparison_fingerprint: provenance.comparison_fingerprint || null,
      runner_commit: runner.commit || null,
    },
    checks: { workflow_run_url: runner.workflow_run_url || null, check_links: runner.check_links || [] },
    scores: scoreSummary(report),
    diagnostics: {
      failure_reason: value.error || report?.outcome?.errors?.[0] || null,
      finding_count: Array.isArray(report?.findings) ? report.findings.length : 0,
      audit_flag_count: Array.isArray(audit?.flags) ? audit.flags.filter((flag) => flag.status === "flagged").length : 0,
      duplicate_model_fingerprint: duplicateModel,
      duplicate_result_fingerprint: duplicateResult,
      comparison_scope: comparisons?.scope || "unknown",
    },
    result_available: Boolean(report),
  };
}

async function operatorDetail(value, store) {
  const comparisons = await store.operatorComparisons();
  return {
    schema_version: "cvbench.operator/v1",
    job: operatorSummary(value, comparisons),
    raw_result: value.result,
    notes: await store.listOperatorNotes(value.id),
  };
}

async function operatorAudit(value, store) {
  const report = value.result;
  const evidence = report?.audit_evidence || null;
  const flags = evidence?.flags ? [...evidence.flags] : [{ id: "run_pending", status: "pending", review_aid_only: true, reason: "No result has been recorded." }];
  if (report) {
    const perfect = scoreSummary(report).perfect;
    const comparisons = await store.operatorComparisons();
    const duplicateModel = duplicateStatus(comparisons, "duplicateImages", value.image);
    const duplicateResult = duplicateStatus(comparisons, "duplicateResults", value.resultSha256);
    flags.push({ id: "duplicate_model_fingerprint", status: duplicateModel === "review" ? "flagged" : duplicateModel, severity: "medium", review_aid_only: true, reason: duplicateModel === "review" ? "Another stored submission uses the same immutable system-image digest." : `System-image comparison status: ${duplicateModel}.` });
    flags.push({ id: "duplicate_result_fingerprint", status: duplicateResult === "review" ? "flagged" : duplicateResult, severity: "medium", review_aid_only: true, reason: duplicateResult === "review" ? "Another stored result has the same canonical report fingerprint." : `Result comparison status: ${duplicateResult}.` });
    if (perfect) flags.push({ id: "score_review", status: "review", severity: "medium", review_aid_only: true, reason: "Perfect scores require human review, not automatic rejection." });
  }
  return {
    schema_version: "cvbench.audit/v1",
    disposition: "review_aid_only",
    automatic_disqualification: false,
    job_id: value.id,
    flags,
    score_components: scoreSummary(report),
    fairness: {
      counted_from: "deterministic frame/target matching",
      explainable_evidence: Boolean(evidence?.frame_samples?.length),
      adjudication: `/api/v1/operator/jobs/${value.id}/notes`,
    },
  };
}

function duplicateStatus(comparisons, field, value) {
  if (!comparisons || !value) return "unknown";
  if (comparisons[field].has(value)) return "review";
  return comparisons.truncated ? "unknown" : "clear";
}

function comparisonSummary(comparisons) {
  return { scope: comparisons.scope, truncated: comparisons.truncated };
}

function operatorEvidence(value) {
  const report = value.result || {};
  return {
    schema_version: "cvbench.audit/v1",
    job_id: value.id,
    audit_evidence: report.audit_evidence || null,
    timing_compute: report.audit_evidence?.timing_compute || null,
    artifacts: [],
    raw_evidence_available: report.provenance?.raw_evidence_available === true,
    bounded_audit_evidence_sha256: report.provenance?.bounded_audit_evidence_sha256 || null,
    bounded_audit_evidence_hash_algorithm: report.provenance?.bounded_audit_evidence_hash_algorithm || "sha256(cvbench.canonical-json/v1)",
    raw_artifact_policy: "Raw ground-truth and submitted-system-output artifacts are not uploaded or exposed by this public repository; only bounded authenticated audit evidence and integrity hashes are retained.",
  };
}

function scoreSummary(report) {
  const metrics = report?.metrics || {};
  const sampleCounts = metrics.sample_counts || {};
  return {
    sample_counts: sampleCounts,
    acquisition_rate: metrics.acquisition?.rate ?? null,
    observed_coverage: metrics.coverage?.overall_observed ?? null,
    continuity_coverage: metrics.coverage?.overall_continuity ?? null,
    mean_iou: metrics.localization?.mean_iou ?? null,
    id_switches: metrics.identity?.id_switches ?? null,
    false_track_births: metrics.false_detections?.track_births ?? null,
    reacquisition_same_id_rate: metrics.reacquisition?.same_id_rate ?? null,
    latency_p50_ms: metrics.latency?.median ?? null,
    latency_p99_ms: metrics.latency?.p99 ?? null,
    native_source_duration_seconds: report?.timing?.source?.duration_seconds ?? null,
    wall_seconds: report?.timing?.durations?.wall_seconds ?? null,
    startup_seconds: report?.timing?.durations?.startup_seconds ?? null,
    stream_delivery_seconds: report?.timing?.durations?.stream_delivery_seconds ?? null,
    completion_seconds: report?.timing?.durations?.completion_seconds ?? null,
    drain_seconds: report?.timing?.durations?.drain_seconds ?? null,
    teardown_seconds: report?.timing?.durations?.teardown_seconds ?? null,
    replay_profile: report?.timing?.replay?.profile ?? null,
    replay_rate: report?.timing?.replay?.rate ?? null,
    effective_replay_rate: report?.timing?.delivery?.effective_replay_rate ?? null,
    delivered_frames_per_second: report?.timing?.delivery?.delivered_frames_per_second ?? null,
    cpu_time_seconds: report?.resources?.cpu_time_seconds ?? null,
    cpu_seconds_per_native_source_second: report?.resources?.cpu_seconds_per_native_source_second ?? null,
    real_time_factor: report?.timing?.durations?.real_time_factor ?? null,
    average_cpu_percent: report?.resources?.average_cpu_percent ?? null,
    peak_cpu_percent: report?.resources?.peak_cpu_percent ?? null,
    peak_ram_bytes: report?.resources?.peak_ram_bytes ?? null,
    disk_read_bytes: report?.resources?.disk_read_bytes ?? null,
    disk_write_bytes: report?.resources?.disk_write_bytes ?? null,
    output_records_per_native_source_second: report?.timing?.output?.records_per_native_source_second ?? null,
    processing_latency_p95_ms: report?.timing?.processing_latency_ms?.p95 ?? null,
    native_source_offset_p95_ms: report?.timing?.native_source_offset_ms?.p95 ?? null,
    delivery_backlog_max_ms: report?.timing?.delivery?.delivery_backlog_ms?.maximum ?? null,
    delivery_deadline_missed_frames: report?.timing?.delivery?.deadline_missed_frames ?? null,
    leaderboard_class: report?.leaderboard?.class_id ?? null,
    leaderboard_eligible: report?.leaderboard?.eligible ?? false,
    leaderboard_disqualifications: report?.leaderboard?.disqualifications ?? [],
    accounting_complete: report?.resources?.authoritative === true
      && Object.values(report?.resources?.accounting_availability || {}).every((value) => value === true),
    perfect: metrics.coverage?.overall_observed === 1 && metrics.localization?.mean_iou === 1 && metrics.identity?.id_switches === 0 && metrics.false_detections?.track_births === 0,
  };
}

function publicResultSummary(report) {
  if (!report) return null;
  return {
    outcome: report.outcome ? { status: report.outcome.status, exit_code: report.outcome.exit_code ?? null } : null,
    benchmark: report.benchmark || null,
    scores: scoreSummary(report),
    findings: Array.isArray(report.findings)
      ? report.findings.map((finding) => ({ finding_id: finding.finding_id, category: finding.category, severity: finding.severity, statement: finding.interpretation?.statement || null }))
      : [],
    provenance: {
      comparison_fingerprint: report.provenance?.comparison_fingerprint || null,
      resolved_container_image: report.provenance?.resolved_container_image || null,
      timing_compute_contract: report.provenance?.timing_compute_contract || null,
      delivery_policy: report.provenance?.delivery_policy || null,
      replay_profile: report.provenance?.replay_profile || null,
      replay_rate: report.provenance?.replay_rate ?? null,
      leaderboard_class: report.provenance?.leaderboard_class || null,
    },
  };
}

function validateOperatorNote(value) {
  if (!isObject(value)) return { error: "Request body must be a JSON object." };
  if (unknownKeys(value, ["verdict", "note"]).length) return { error: "Only verdict and note are accepted." };
  if (!["unreviewed", "needs_review", "adjudicated", "accepted", "rejected"].includes(value.verdict)) return { error: "verdict is invalid." };
  const note = cleanText(value.note, "note", 1, 4000);
  if (note.error) return note;
  return { value: { verdict: value.verdict, note: note.value } };
}

function cleanActorId(value) {
  const actor = String(value || "unattributed-operator").trim().toLowerCase();
  return /^[A-Za-z0-9._:@/-]{1,100}$/.test(actor) && !/^(?:unattributed|legacy)(?:[._:@/-]|$)/i.test(actor) ? actor : null;
}

function parseAdjudicatorCredentials(value) {
  let parsed = value;
  if (typeof value === "string") {
    try {
      if (hasDuplicateJsonKeys(value)) return [];
      parsed = JSON.parse(value);
    } catch {
      return [];
    }
  }
  if (!isObject(parsed) || Array.isArray(parsed)) return [];
  const seenActors = new Set();
  const seenTokens = new Set();
  const credentials = [];
  for (const [actorId, token] of Object.entries(parsed)) {
    const cleanActor = cleanActorId(actorId);
    if (!cleanActor || seenActors.has(cleanActor) || typeof token !== "string" || token.length === 0 || seenTokens.has(token)) return [];
    seenActors.add(cleanActor);
    seenTokens.add(token);
    credentials.push({ actorId: cleanActor, token });
  }
  return credentials;
}

function hasDuplicateJsonKeys(value) {
  const seen = new Set();
  for (const match of value.matchAll(/"((?:\\.|[^"\\])*)"\s*:/g)) {
    const key = JSON.parse(`"${match[1]}"`);
    if (seen.has(key)) return true;
    seen.add(key);
  }
  return false;
}

function parseCursor(value) {
  if (!value) return null;
  const match = value.match(/^(\d+):([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/);
  return match ? { createdAt: Number(match[1]), id: match[2] } : null;
}

function encodeCursor(value) {
  return value ? `${value.createdAt}:${value.id}` : null;
}

function runnerSubmission(value) {
  return {
    id: value.id,
    image: value.image,
    argv: value.argv,
    model: { name: value.name, version: value.modelVersion },
    benchmark: PUBLIC_BENCHMARK,
    attempt: value.attempt,
  };
}

function matchesPublicBenchmark(value) {
  return isObject(value) && value.id === PUBLIC_BENCHMARK.id && value.version === PUBLIC_BENCHMARK.version;
}

function matchesPublicTimingContract(report) {
  return (
    report?.provenance?.timing_compute_contract === PUBLIC_BENCHMARK.timing_compute_contract
    && report?.provenance?.delivery_policy === PUBLIC_BENCHMARK.delivery_policy
    && report?.provenance?.replay_profile === PUBLIC_BENCHMARK.replay_profile
    && report?.provenance?.replay_rate === PUBLIC_BENCHMARK.replay_rate
    && report?.leaderboard?.policy_version === PUBLIC_BENCHMARK.leaderboard_policy
    && report?.leaderboard?.replay_class === PUBLIC_BENCHMARK.replay_profile
  );
}

async function readJson(request, maxBytes) {
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > maxBytes) return { error: problem(413, "payload_too_large", `Payload limit is ${maxBytes} bytes.`) };
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > maxBytes) {
    return { error: problem(413, "payload_too_large", `Payload limit is ${maxBytes} bytes.`) };
  }
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { error: problem(400, "invalid_json", "Request body must be valid JSON.") };
  }
}

async function authorized(candidate, expectedTokens) {
  if (!candidate || expectedTokens.length === 0 || expectedTokens.every((value) => !value)) return false;
  const candidateDigest = await digest(candidate);
  let match = 0;
  for (const token of expectedTokens) {
    const expectedDigest = await digest(token || "invalid-placeholder");
    match |= constantTimeEqual(candidateDigest, expectedDigest) ? 1 : 0;
  }
  return match === 1;
}

async function actorForCredential(candidate, credentials) {
  if (!candidate || credentials.length === 0) return null;
  const candidateDigest = await digest(candidate);
  let actorId = null;
  let matches = 0;
  for (const credential of credentials) {
    const expectedDigest = await digest(credential.token || "invalid-placeholder");
    if (constantTimeEqual(candidateDigest, expectedDigest)) {
      matches += 1;
      actorId = credential.actorId;
    }
  }
  return matches === 1 ? actorId : null;
}

function constantTimeEqual(left, right) {
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) difference |= (left[index % left.length] ^ right[index % right.length]);
  return difference === 0;
}

async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

async function sha256(value) {
  return [...await digest(value)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function bearerToken(request) {
  const match = request.headers.get("authorization")?.match(/^Bearer ([!-~]{1,512})$/);
  return match ? match[1] : "";
}

function cleanText(value, name, minimum, maximum) {
  if (typeof value !== "string") return { error: `${name} must be a string.` };
  const clean = value.trim();
  if (clean.length < minimum || clean.length > maximum || /[\0-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(clean)) {
    return { error: `${name} must contain ${minimum}-${maximum} safe characters.` };
  }
  return { value: clean };
}

function optionalText(value, name, maximum) {
  if (value === undefined || value === null || value === "") return { value: null };
  return cleanText(value, name, 1, maximum);
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (isObject(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function stableJson(value) {
  return canonicalJson(value);
}

function splitKeys(value) {
  return String(value || "").split(",").map((key) => key.trim()).filter(Boolean);
}

function uniqueCredentialScopes(scopes) {
  const seen = new Set();
  for (const tokens of Object.values(scopes)) {
    for (const token of tokens) {
      if (!token || seen.has(token)) return false;
      seen.add(token);
    }
  }
  return true;
}

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number.parseInt(String(value ?? fallback), 10);
  return Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function unknownKeys(value, allowed) {
  return Object.keys(value).filter((key) => !allowed.includes(key));
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function randomToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function unixTime() {
  return Math.floor(Date.now() / 1000);
}

function iso(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toISOString() : null;
}

function unauthorized(kind) {
  return problem(401, "unauthorized", `A valid ${kind} is required.`, { "www-authenticate": "Bearer" });
}

function problem(status, code, message, headers = {}) {
  return json({ error: { code, message } }, status, headers);
}

function json(value, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(value, null, 2), { status, headers: { ...JSON_HEADERS, ...extraHeaders } });
}

export const CONTRACT = {
  schema_version: "cvbench.contract/v1",
  benchmark: {
    ...PUBLIC_BENCHMARK,
    selection: "Every public v1 submission runs this fixed versioned suite; the v1 request does not select or override a benchmark.",
    composition: "13 deterministic synthetic scenarios plus 3 dense, full-frame real-video multi-object tracking scenarios.",
    measures: ["class-aware detection and association", "HOTA", "IDF1", "misses", "false tracks", "ID switches", "fragmentation", "track completeness", "latency", "resource use", "diagnostics"],
    input: "Progressive timestamped JPEG frames over a Unix-domain socket; the system under test never receives future frames.",
    temporal_support: "The socket stays open across ordered frames. Detector, tracker, temporal-memory, association, filtering, and post-processing pipelines may retain multi-frame state and may use multiple processes.",
    timing_compute: {
      source_time: "Native source timestamps, FPS, and duration are immutable. Replay pace never rewrites camera truth.",
      replay: "Public /api/v1 submissions are fixed to the allowlisted native profile at exactly 1.0x. The v1 request has no replay override; slower allowlisted profiles are separate benchmark classes, never native results.",
      delivery: "An independent monotonic source schedule preserves ordering and never reveals future frames. The lossless-v1 policy reports sender pressure, delivery backlog, deadline misses, and explicit fault drops.",
      completion: "Startup, stream delivery, bounded post-stream drain, wall duration, per-output processing latency, late-output counts, and out-of-band teardown are reported separately.",
      compute: "The trusted host reads cgroup-v2 CPU time, CPU-seconds per native source-second, average/peak CPU, peak RAM, disk I/O, process count, and a final cumulative sample without executing inside the submitted image. Missing axes are ineligible. GPU/VRAM are omitted unless an isolated device is assigned.",
      fairness: "cvbench.pareto/v1 has no hidden composite. Accuracy remains intact; replay profile, compute tier, completion tier, and raw efficiency axes define the leaderboard class.",
    },
  },
  container: {
    image: "Prebuilt OCI image pinned as registry/repository@sha256:<64 lowercase hex characters>.",
    platform: "linux/amd64",
    network: "disabled",
    filesystem: "container default plus one read/write progressive socket-directory mount at /run/cvbench; no extra mounts and no Docker socket",
    user: "unprivileged host-aligned numeric UID/GID",
    environment: { CVBENCH_INPUT_SOCKET: "/run/cvbench/input.sock" },
    readiness: "Print exactly CVBENCH_READY as a stdout line after connecting.",
    output: "Then print one cvbench.track/v1 JSON object per stdout line; diagnostics go to stderr.",
    resources: { cpus: 4, memory_mb: 2048 },
  },
  submission: {
    accepted: ["image", "argv", "name", "model_version", "contact", "notes"],
    rejected: ["source repositories", "build instructions", "shell command strings", "Docker socket access", "custom environment variables", "replay or pacing overrides"],
    authentication: "Bearer submission API key plus a unique Idempotency-Key header.",
    terminology: "The submitted object is a complete system image. One linux/amd64 OCI image is a packaging, reproducibility, and security boundary, not a one-learned-model or one-process assumption.",
    compatibility_names: {
      model_version: "Version 1 wire/storage field retained for compatibility; it means system version.",
      response_model: "Version 1 response object named model retained for compatibility; it describes the submitted system.",
      internal_modelVersion: "Version 1 implementation/storage compatibility name for system version.",
      d1_model_version: "Version 1 D1 column retained for compatibility; it stores the submitted system version.",
    },
  },
};

export const OPENAPI = {
  openapi: "3.1.0",
  info: { title: "CVBench Control Plane API", version: "1.0.0", description: "Submit one immutable linux/amd64 OCI image containing a complete vision system. Every v1 submission runs the fixed public-whole-system-tracking Version 2 suite of 13 synthetic and 3 dense full-frame real-video scenarios." },
  "x-cvbench-public-benchmark": PUBLIC_BENCHMARK,
  servers: [{ url: "/" }],
  paths: {
    "/api/v1/health": { get: { operationId: "health", responses: { 200: { description: "Healthy" }, 503: { description: "D1 unavailable" } } } },
    "/api/v1/contract": { get: { operationId: "getContract", responses: { 200: { description: "Benchmark and container contract" } } } },
    "/api/v1/openapi.json": { get: { operationId: "getOpenApi", responses: { 200: { description: "This document" } } } },
    "/api/v1/submissions": {
      post: {
        operationId: "createSubmission",
        security: [{ submissionKey: [] }],
        parameters: [{ name: "Idempotency-Key", in: "header", required: true, schema: { type: "string", minLength: 8, maxLength: 128 } }],
        requestBody: { required: true, content: { "application/json": { schema: { $ref: "#/components/schemas/CreateSubmission" } } } },
        responses: { 201: { description: "Queued" }, 200: { description: "Idempotent replay" }, 401: { description: "Unauthorized" }, 422: { description: "Invalid submission" }, 429: { description: "Hourly limit reached" } },
      },
    },
    "/api/v1/submissions/{id}": {
      get: {
        operationId: "getSubmission",
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        responses: {
          200: {
            description: "Public submission status/result with raw accuracy and timing/compute axes",
            content: {
              "application/json": {
                schema: {
                  type: "object",
                  properties: {
                    result: {
                      type: ["object", "null"],
                      properties: {
                        scores: { $ref: "#/components/schemas/TimingComputeSummary" },
                      },
                    },
                  },
                },
              },
            },
          },
          404: { description: "Not found" },
        },
      },
    },
    "/api/v1/internal/submissions/{id}/result": {
      post: {
        operationId: "completeRunnerSubmission",
        security: [{ runnerKey: [] }],
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        requestBody: {
          required: true,
          content: {"application/json": {schema: {$ref: "#/components/schemas/RunnerResult"}}},
        },
        responses: {
          200: {description: "Lease completed"},
          409: {description: "Lease conflict"},
          422: {description: "Invalid or incomplete report"},
        },
      },
    },
    "/api/v1/operator/jobs": {
      get: {
        operationId: "listOperatorJobs",
        security: [{ operatorReadKey: [] }],
        parameters: [
          { name: "status", in: "query", schema: { type: "string", enum: ["queued", "running", "succeeded", "failed"] } },
          { name: "model", in: "query", description: "Version 1 compatibility query name; filters by submitted system name.", schema: { type: "string", maxLength: 100 } },
          { name: "limit", in: "query", schema: { type: "integer", minimum: 1, maximum: 100, default: 25 } },
          { name: "cursor", in: "query", schema: { type: "string" } },
        ],
        responses: { 200: { description: "Operator queue page with a stable next cursor" }, 401: { description: "Operator token required" } },
      },
    },
    "/api/v1/operator/jobs/{id}": {
      get: {
        operationId: "getOperatorJob",
        security: [{ operatorReadKey: [] }],
        parameters: [{ name: "id", in: "path", required: true, schema: { type: "string", format: "uuid" } }],
        responses: { 200: { description: "Operator detail and raw bounded report" }, 401: { description: "Operator token required" } },
      },
    },
    "/api/v1/operator/jobs/{id}/audit": {
      get: { operationId: "getOperatorAudit", security: [{ operatorReadKey: [] }], responses: { 200: { description: "Review-only anomaly flags and fairness explanation" } } },
    },
    "/api/v1/operator/jobs/{id}/evidence": {
      get: { operationId: "getOperatorEvidence", security: [{ operatorReadKey: [] }], responses: { 200: { description: "Bounded sampled frame evidence, integrity hash, and explicit raw-evidence availability" } } },
    },
    "/api/v1/operator/jobs/{id}/notes": {
      get: { operationId: "listOperatorNotes", security: [{ operatorReadKey: [] }], responses: { 200: { description: "Adjudication trail" } } },
      post: { operationId: "addOperatorNote", security: [{ operatorAdjudicatorKey: [] }], responses: { 201: { description: "Appended operator verdict note with configured actor attribution" } } },
    },
  },
  components: {
    securitySchemes: {
      submissionKey: { type: "http", scheme: "bearer" },
      runnerKey: { type: "http", scheme: "bearer" },
      operatorReadKey: { type: "http", scheme: "bearer", description: "Least-privilege operator read credential; never the submission, adjudicator, or runner token." },
      operatorAdjudicatorKey: { type: "http", scheme: "bearer", description: "Credential mapped to one stable actor identity; it cannot read operator routes and is never exposed or stored as a bearer value." },
    },
    schemas: {
      CreateSubmission: {
        type: "object",
        additionalProperties: false,
        required: ["image", "argv", "name", "model_version"],
        properties: {
          image: { type: "string", pattern: "@sha256:[a-f0-9]{64}$" },
          argv: { type: "array", minItems: 1, maxItems: 32, items: { type: "string", minLength: 1, maxLength: 256 } },
          name: { type: "string", maxLength: 100 },
          model_version: { type: "string", maxLength: 100, description: "Version 1 compatibility field containing the submitted system version." },
          contact: { type: "string", maxLength: 200 },
          notes: { type: "string", maxLength: 1000 },
        },
      },
      TimingComputeSummary: {
        type: "object",
        additionalProperties: false,
        properties: {
          sample_counts: { type: "object" },
          acquisition_rate: { type: ["number", "null"] },
          observed_coverage: { type: ["number", "null"] },
          continuity_coverage: { type: ["number", "null"] },
          mean_iou: { type: ["number", "null"] },
          id_switches: { type: ["number", "null"] },
          false_track_births: { type: ["number", "null"] },
          reacquisition_same_id_rate: { type: ["number", "null"] },
          latency_p50_ms: { type: ["number", "null"], minimum: 0 },
          latency_p99_ms: { type: ["number", "null"], minimum: 0 },
          native_source_duration_seconds: { type: ["number", "null"], minimum: 0 },
          wall_seconds: { type: ["number", "null"], minimum: 0 },
          startup_seconds: { type: ["number", "null"], minimum: 0 },
          stream_delivery_seconds: { type: ["number", "null"], minimum: 0 },
          completion_seconds: { type: ["number", "null"], minimum: 0 },
          drain_seconds: { type: ["number", "null"], minimum: 0 },
          teardown_seconds: { type: ["number", "null"], minimum: 0 },
          replay_profile: { type: ["string", "null"], enum: ["native", null] },
          replay_rate: { type: ["number", "null"], enum: [1, null] },
          effective_replay_rate: { type: ["number", "null"], minimum: 0 },
          delivered_frames_per_second: { type: ["number", "null"], minimum: 0 },
          cpu_time_seconds: { type: ["number", "null"], minimum: 0 },
          cpu_seconds_per_native_source_second: { type: ["number", "null"], minimum: 0 },
          real_time_factor: { type: ["number", "null"], minimum: 0 },
          average_cpu_percent: { type: ["number", "null"], minimum: 0 },
          peak_cpu_percent: { type: ["number", "null"], minimum: 0 },
          peak_ram_bytes: { type: ["number", "null"], minimum: 0 },
          disk_read_bytes: { type: ["number", "null"], minimum: 0 },
          disk_write_bytes: { type: ["number", "null"], minimum: 0 },
          output_records_per_native_source_second: { type: ["number", "null"], minimum: 0 },
          processing_latency_p95_ms: { type: ["number", "null"], minimum: 0 },
          native_source_offset_p95_ms: { type: ["number", "null"] },
          delivery_backlog_max_ms: { type: ["number", "null"], minimum: 0 },
          delivery_deadline_missed_frames: { type: ["integer", "null"], minimum: 0 },
          leaderboard_class: { type: ["string", "null"] },
          leaderboard_eligible: { type: "boolean" },
          leaderboard_disqualifications: { type: "array", items: { type: "string" } },
          accounting_complete: { type: "boolean" },
          perfect: { type: "boolean" },
        },
      },
      RunnerResult: {
        oneOf: [
          {
            type: "object",
            additionalProperties: false,
            required: ["status", "lease_token", "report"],
            properties: {
              status: { const: "succeeded" },
              lease_token: { type: "string", minLength: 32, maxLength: 200 },
              report: { $ref: "#/components/schemas/ReportV1" },
            },
          },
          {
            type: "object",
            additionalProperties: false,
            required: ["status", "lease_token", "error"],
            properties: {
              status: { const: "failed" },
              lease_token: { type: "string", minLength: 32, maxLength: 200 },
              error: { type: "string", minLength: 1, maxLength: 2000 },
            },
          },
        ],
      },
      TimingComputeV1: OPENAPI_TIMING_COMPUTE_SCHEMA,
      ReportV1: OPENAPI_REPORT_SCHEMA,
    },
  },
};
