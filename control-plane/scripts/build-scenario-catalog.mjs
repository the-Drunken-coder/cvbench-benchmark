#!/usr/bin/env node

import { createHash } from "node:crypto";
import { cp, lstat, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { parse as parseYaml } from "yaml";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTROL_PLANE = path.resolve(HERE, "..");
const ROOT = path.resolve(CONTROL_PLANE, "..");
const PUBLIC = path.join(CONTROL_PLANE, "public");
const CATALOG_SOURCE = path.join(ROOT, "scenario-catalog");
const DEFAULT_OUTPUT = path.join(CONTROL_PLANE, "dist");
const STATIC_FILES = [
  "_headers",
  "app.js",
  "404.html",
  "index.html",
  "operator.html",
  "operator.js",
  "scenario-app.js",
  "scenario-loader.js",
  "scenarios/index.html",
  "styles.css",
];
const BENCHMARK_FILES = [
  "benchmarks/long-running-stability.yaml",
  "benchmarks/persistent-target-tracking.yaml",
  "benchmarks/public-whole-system-v2.yaml",
  "benchmarks/real-video-v2.yaml",
];
const ALLOWED_PUBLISHED_EXTENSIONS = new Set(["", ".css", ".html", ".jpg", ".js", ".json"]);
const PRIVATE_PATH_PATTERN = /(?:^|\/)(?:\.dev\.vars|\.env(?:\.|$)|.*(?:credential|secret|contact|note|failure[-_]?packet|raw[-_]?report|d1[-_]?export|private[-_]?log).*)(?:\/|$)/i;
const PRIVATE_FIELD_PATTERN = /(?:api[-_]?key|token|secret|credential|password|private|local[-_]?path|absolute[-_]?path|contact|notes?|failure[-_]?packet|raw[-_]?report|d1[-_]?(?:export|internal|database|binding)|operator|lease)/i;
const PRIVATE_CONTENT_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /(?:SUBMISSION_API_KEYS|RUNNER_TOKEN|OPERATOR_READ_API_KEYS|OPERATOR_ADJUDICATOR_CREDENTIALS)\s*=/,
  /"(?:contact|notes|lease_token|submitter_key_hash|operator_key_hash)"\s*:/i,
];
const MAX_ASSET_BYTES = 25 * 1024 * 1024;
const RECOMMENDED_FRAME_BYTES = 2 * 1024 * 1024;
const MAX_CATALOG_BYTES = 256 * 1024;
const MAX_SCENARIO_BYTES = 512 * 1024;
const MAX_SITE_BYTES = 50 * 1024 * 1024;
const OUTPUT_DIRECTORY_PATTERN = /^dist(?:-test-[a-z0-9][a-z0-9-]*)?$/;
const ANNOTATION_FIELDS = new Set([
  "bbox_xyxy",
  "class_id",
  "eligible_for_detection",
  "entry_event",
  "exit_event",
  "ignore",
  "ignore_region",
  "ignore_region_id",
  "occlusion",
  "on_screen",
  "reappearance_event",
  "sequence_id",
  "schema_version",
  "source_timestamp_ns",
  "target_id",
  "truncated",
  "visibility_fraction",
  "vision_loss_interval",
]);
const SOURCE_FIELDS = new Set([
  "annotation_geom_sha256",
  "annotation_provenance",
  "annotation_types_sha256",
  "annotation_url",
  "attribution",
  "corrections",
  "creator",
  "dataset",
  "dataset_version",
  "frame_range",
  "license",
  "license_url",
  "native_fps",
  "sequence",
  "source_sha256",
  "source_url",
  "title",
  "transformation",
]);
const PREPARATION_FIELDS = new Set(["base_image", "expected_frame_manifest", "identity", "platform", "toolchain"]);
const SCENARIO_METADATA_FIELDS = new Set(["description", "failure_modes", "title"]);
const METRIC_FIELDS = new Set([
  "association_accuracy",
  "false_detections",
  "hota",
  "idf1",
  "neutral_ignored_predictions",
  "observed_coverage",
  "state_contamination_events",
  "track_id_exhaustion_detected",
  "track_id_reuse_events",
]);
const PREPARATION_HASH = sha256(Buffer.from(await readFile(path.join(ROOT, "examples/Dockerfile.real-video-prep"))));

function fail(message) {
  throw new Error(`scenario catalog build rejected: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value) {
  return `${JSON.stringify(sortValue(value), null, 2)}\n`;
}

function sortValue(value) {
  if (Array.isArray(value)) return value.map(sortValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortValue(value[key])]));
}

function plainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) {
    fail(`${label} must be a plain object`);
  }
  return value;
}

function rejectPrivateFields(value, label) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => rejectPrivateFields(item, `${label}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (PRIVATE_FIELD_PATTERN.test(key)) fail(`${label} contains private-looking field ${key}`);
    rejectPrivateFields(child, `${label}.${key}`);
  }
}

function allowedObject(value, allowedFields, label) {
  plainObject(value, label);
  rejectPrivateFields(value, label);
  for (const key of Object.keys(value)) {
    if (!allowedFields.has(key)) fail(`${label} contains undeclared field ${key}`);
  }
  return value;
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) fail(`${label} must be a non-empty string`);
  return value;
}

function requiredSha256(value, label) {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) fail(`${label} must be a lowercase sha256`);
  return value;
}

async function assertNoSymlinkComponents(file, allowedRoot) {
  const relative = path.relative(allowedRoot, file);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) fail(`path escapes allowlisted root: ${file}`);
  let current = allowedRoot;
  const rootInfo = await lstat(current);
  if (rootInfo.isSymbolicLink()) fail(`allowlisted root is a symlink: ${allowedRoot}`);
  for (const component of relative.split(path.sep)) {
    current = path.join(current, component);
    const info = await lstat(current);
    if (info.isSymbolicLink()) fail(`symlink is not publishable: ${path.relative(allowedRoot, current)}`);
  }
}

async function readYaml(relative) {
  return parseYaml(await readFile(path.join(ROOT, relative), "utf8"));
}

async function assertedRegularFile(file, allowedRoot) {
  const relative = path.relative(allowedRoot, file);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) fail(`path escapes allowlisted root: ${file}`);
  if (PRIVATE_PATH_PATTERN.test(relative.replaceAll(path.sep, "/"))) fail(`private artifact pattern: ${relative}`);
  await assertNoSymlinkComponents(file, allowedRoot);
  const info = await lstat(file);
  if (!info.isFile()) fail(`declared asset is not a regular file: ${relative}`);
  return info;
}

function assertSafeOutput(output) {
  const relative = path.relative(CONTROL_PLANE, output);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    fail("output must be a dedicated directory below control-plane");
  }
  if (path.dirname(relative) !== "." || !OUTPUT_DIRECTORY_PATTERN.test(path.basename(relative))) {
    fail("output must be control-plane/dist or a direct dist-test-* directory");
  }
  return output;
}

function finiteNumber(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(`${label} must be finite`);
}

function validateBox(box, width, height, label) {
  if (!Array.isArray(box) || box.length !== 4) fail(`${label} must contain four coordinates`);
  box.forEach((value, index) => finiteNumber(value, `${label}[${index}]`));
  const [x1, y1, x2, y2] = box;
  if (x1 < 0 || y1 < 0 || x2 > width || y2 > height || x2 <= x1 || y2 <= y1) {
    fail(`${label} is outside ${width}x${height}`);
  }
}

function sanitizeAnnotation(row, manifest, index) {
  const label = `${manifest.id} annotation ${index}`;
  allowedObject(row, ANNOTATION_FIELDS, label);
  if ("schema_version" in row && row.schema_version !== "cvbench.ground-truth/v1") fail(`${label}.schema_version is invalid`);
  requiredString(row.sequence_id, `${label}.sequence_id`);
  requiredString(row.target_id, `${label}.target_id`);
  requiredString(row.class_id, `${label}.class_id`);
  if (manifest.ontology !== undefined && (!Array.isArray(manifest.ontology) || !manifest.ontology.includes(row.class_id))) {
    fail(`${label}.class_id is outside the scenario ontology`);
  }
  if (!Number.isInteger(row.source_timestamp_ns) || row.source_timestamp_ns < 0) fail(`${label}.source_timestamp_ns must be a non-negative integer`);
  if (typeof row.on_screen !== "boolean" || typeof row.eligible_for_detection !== "boolean") fail(`${label} has invalid eligibility flags`);
  requiredString(row.occlusion, `${label}.occlusion`);
  if (row.visibility_fraction === null) {
    if (row.occlusion !== "unknown") fail(`${label} unknown visibility requires unknown occlusion`);
  } else {
    finiteNumber(row.visibility_fraction, `${label}.visibility_fraction`);
    if (row.visibility_fraction < 0 || row.visibility_fraction > 1) fail(`${label}.visibility_fraction must be between zero and one`);
  }
  for (const field of ["entry_event", "exit_event", "ignore", "ignore_region", "reappearance_event", "truncated", "vision_loss_interval"]) {
    if (field in row && typeof row[field] !== "boolean") fail(`${label}.${field} must be boolean`);
  }
  if ("ignore_region_id" in row) requiredString(row.ignore_region_id, `${label}.ignore_region_id`);
  if (row.ignore_region && (!row.ignore || !row.ignore_region_id || !row.bbox_xyxy)) fail(`${label} has an incomplete ignore region`);
  if (row.ignore_region_id && !row.ignore_region) fail(`${label}.ignore_region_id requires ignore_region=true`);
  if (row.on_screen && !row.bbox_xyxy) fail(`${label} requires bbox_xyxy while on screen`);
  if (row.bbox_xyxy) {
    const frame = manifest.frames.find((candidate) => candidate.source_timestamp_ns === row.source_timestamp_ns);
    if (!frame) fail(`${label} references an undeclared timestamp`);
    validateBox(row.bbox_xyxy, frame.width, frame.height, `${label} bbox`);
  }
  return Object.fromEntries(Object.keys(row).sort().map((key) => [key, Array.isArray(row[key]) ? [...row[key]] : row[key]]));
}

function sanitizeFault(fault, id, index) {
  const label = `${id} fault ${index}`;
  allowedObject(fault, new Set(["after_frame", "duration_ms", "frame_indices", "type"]), label);
  requiredString(fault.type, `${label}.type`);
  if ("after_frame" in fault && (!Number.isInteger(fault.after_frame) || fault.after_frame < 0)) fail(`${label}.after_frame must be a non-negative integer`);
  if ("duration_ms" in fault && (!Number.isInteger(fault.duration_ms) || fault.duration_ms < 0)) fail(`${label}.duration_ms must be a non-negative integer`);
  if ("frame_indices" in fault && (!Array.isArray(fault.frame_indices) || !fault.frame_indices.every((value) => Number.isInteger(value) && value >= 0))) {
    fail(`${label}.frame_indices must contain non-negative integers`);
  }
  return Object.fromEntries(Object.keys(fault).sort().map((key) => [key, Array.isArray(fault[key]) ? [...fault[key]] : fault[key]]));
}

function sanitizeMetadata(metadata, ids) {
  allowedObject(metadata, new Set(["catalog", "preparation", "scenarios", "schema_version", "sources"]), "catalog metadata");
  if (metadata.schema_version !== "cvbench.scenario-catalog-metadata/v1") fail("invalid catalog metadata schema");
  allowedObject(metadata.catalog, new Set(["author", "description", "status", "title", "version"]), "catalog metadata.catalog");
  if (metadata.catalog.status !== "public") fail("catalog status must be public");
  for (const field of ["author", "description", "title", "version"]) requiredString(metadata.catalog[field], `catalog metadata.catalog.${field}`);
  allowedObject(metadata.preparation, PREPARATION_FIELDS, "catalog metadata.preparation");
  for (const field of PREPARATION_FIELDS) requiredString(metadata.preparation[field], `catalog metadata.preparation.${field}`);
  plainObject(metadata.sources, "catalog metadata.sources");
  const expectedSources = ["rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6", "synthetic"];
  if (JSON.stringify(Object.keys(metadata.sources).sort()) !== JSON.stringify(expectedSources)) fail("catalog metadata sources must match the public source allowlist");
  const sources = {};
  for (const key of expectedSources) {
    const source = allowedObject(metadata.sources[key], SOURCE_FIELDS, `catalog metadata.sources.${key}`);
    for (const field of ["attribution", "creator", "license", "source_url", "title", "transformation"]) {
      requiredString(source[field], `catalog metadata.sources.${key}.${field}`);
    }
    if (key !== "synthetic") {
      for (const field of ["annotation_geom_sha256", "annotation_provenance", "annotation_types_sha256", "annotation_url", "corrections", "dataset", "dataset_version", "frame_range", "native_fps", "sequence"]) {
        requiredString(source[field], `catalog metadata.sources.${key}.${field}`);
      }
    }
    if ("license_url" in source) requiredString(source.license_url, `catalog metadata.sources.${key}.license_url`);
    if ("source_sha256" in source) requiredSha256(source.source_sha256, `catalog metadata.sources.${key}.source_sha256`);
    sources[key] = Object.fromEntries(Object.keys(source).sort().map((field) => [field, source[field]]));
  }
  plainObject(metadata.scenarios, "catalog metadata.scenarios");
  if (JSON.stringify(Object.keys(metadata.scenarios).sort()) !== JSON.stringify([...ids].sort())) fail("curated metadata must match the benchmark scenario union exactly");
  const scenarios = {};
  for (const id of ids) {
    const entry = allowedObject(metadata.scenarios[id], SCENARIO_METADATA_FIELDS, `catalog metadata.scenarios.${id}`);
    requiredString(entry.title, `catalog metadata.scenarios.${id}.title`);
    requiredString(entry.description, `catalog metadata.scenarios.${id}.description`);
    if (!Array.isArray(entry.failure_modes) || !entry.failure_modes.length) fail(`missing curated metadata for ${id}`);
    entry.failure_modes.forEach((value, index) => requiredString(value, `catalog metadata.scenarios.${id}.failure_modes[${index}]`));
    scenarios[id] = { title: entry.title, description: entry.description, failure_modes: [...entry.failure_modes] };
  }
  return { catalog: { ...metadata.catalog }, preparation: { ...metadata.preparation }, scenarios, sources };
}

function jpegDimensions(buffer) {
  if (buffer[0] !== 0xff || buffer[1] !== 0xd8) fail("frame is not a JPEG");
  let offset = 2;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) fail("malformed JPEG marker stream");
    const marker = buffer[offset + 1];
    offset += 2;
    if (marker === 0xd8 || marker === 0xd9) continue;
    const length = buffer.readUInt16BE(offset);
    if (length < 2 || offset + length > buffer.length) fail("malformed JPEG segment length");
    if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
      return { height: buffer.readUInt16BE(offset + 3), width: buffer.readUInt16BE(offset + 5) };
    }
    offset += length;
  }
  fail("JPEG dimensions are missing");
}

async function benchmarkInventory() {
  const scenarioPaths = new Map();
  const benchmarkMembership = new Map();
  for (const relative of BENCHMARK_FILES) {
    const benchmark = await readYaml(relative);
    if (benchmark?.schema_version !== "cvbench.benchmark/v1" || !Array.isArray(benchmark.scenarios)) {
      fail(`invalid benchmark manifest: ${relative}`);
    }
    for (const declared of benchmark.scenarios) {
      const absolute = path.resolve(path.dirname(path.join(ROOT, relative)), declared);
      await assertedRegularFile(absolute, path.join(ROOT, "scenarios"));
      const manifest = parseYaml(await readFile(absolute, "utf8"));
      if (!manifest?.id || manifest.schema_version !== "cvbench.scenario/v1") fail(`invalid scenario manifest: ${declared}`);
      const prior = scenarioPaths.get(manifest.id);
      if (prior && prior !== absolute) fail(`duplicate scenario id ${manifest.id}`);
      scenarioPaths.set(manifest.id, absolute);
      const memberships = benchmarkMembership.get(manifest.id) || [];
      if (!memberships.includes(benchmark.id)) memberships.push(benchmark.id);
      benchmarkMembership.set(manifest.id, memberships);
    }
  }
  if (scenarioPaths.size !== 16) fail(`benchmark union must contain 16 scenarios, found ${scenarioPaths.size}`);
  return { benchmarkMembership, scenarioPaths };
}

async function expectedRealHashes() {
  const expected = new Map();
  const content = await readFile(path.join(ROOT, "scenarios/real-video-v2/expected-frame-sha256.txt"), "utf8");
  for (const line of content.trim().split("\n")) {
    const [digest, relative] = line.split("  ");
    if (!/^[a-f0-9]{64}$/.test(digest) || !relative) fail("malformed canonical real-frame hash manifest");
    expected.set(relative, digest);
  }
  if (expected.size !== 450) fail(`canonical real-frame manifest must contain 450 entries, found ${expected.size}`);
  return expected;
}

function tarString(buffer, start, length) {
  return buffer.subarray(start, start + length).toString("utf8").replace(/\0.*$/s, "").trim();
}

function tarOctal(buffer, start, length, label) {
  const value = tarString(buffer, start, length);
  if (!/^[0-7]+$/.test(value)) fail(`${label} has an invalid tar number`);
  return Number.parseInt(value, 8);
}

function parseFrameTar(buffer, label) {
  const entries = new Map();
  let offset = 0;
  while (offset + 512 <= buffer.length) {
    const header = buffer.subarray(offset, offset + 512);
    if (header.every((value) => value === 0)) {
      if (!buffer.subarray(offset).every((value) => value === 0)) fail(`${label} has nonzero trailing tar bytes`);
      return entries;
    }
    const expectedChecksum = tarOctal(header, 148, 8, label);
    const check = Buffer.from(header);
    check.fill(0x20, 148, 156);
    const actualChecksum = check.reduce((total, value) => total + value, 0);
    if (actualChecksum !== expectedChecksum) fail(`${label} has a bad tar header checksum`);
    const prefix = tarString(header, 345, 155);
    const name = [prefix, tarString(header, 0, 100)].filter(Boolean).join("/");
    if (!/^frames\/frame-[0-9]{4}\.jpg$/.test(name) || entries.has(name)) fail(`${label} has an undeclared tar entry: ${name}`);
    const type = tarString(header, 156, 1);
    if (type && type !== "0") fail(`${label} has a non-regular tar entry: ${name}`);
    const size = tarOctal(header, 124, 12, `${label}/${name}`);
    const start = offset + 512;
    const end = start + size;
    if (end > buffer.length) fail(`${label}/${name} exceeds the tar boundary`);
    entries.set(name, Buffer.from(buffer.subarray(start, end)));
    offset = start + Math.ceil(size / 512) * 512;
  }
  fail(`${label} is missing the tar terminator`);
}

async function loadRealFrameArchives(expectedHashes) {
  const manifestPath = path.join(ROOT, "scenarios/real-video-v2/archives.json");
  await assertedRegularFile(manifestPath, path.join(ROOT, "scenarios"));
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  allowedObject(manifest, new Set(["archives", "frame_count", "schema_version"]), "real-video archive manifest");
  if (manifest.schema_version !== "cvbench.real-video-archives/v1" || manifest.frame_count !== 450) fail("invalid real-video archive manifest");
  plainObject(manifest.archives, "real-video archive manifest.archives");
  const expectedIds = ["rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6"];
  if (JSON.stringify(Object.keys(manifest.archives).sort()) !== JSON.stringify(expectedIds)) fail("real-video archive scenario set mismatch");
  const frames = new Map();
  for (const id of expectedIds) {
    const pair = allowedObject(manifest.archives[id], new Set(["frame_archive", "review_archive"]), `${id} archives`);
    for (const [kind, declaration] of Object.entries(pair)) {
      allowedObject(declaration, new Set(["bytes", "path", "sha256"]), `${id} ${kind}`);
      const suffix = kind === "frame_archive" ? "frames.tar" : "visual-audit.tar";
      const expectedPath = `scenarios/real-video-v2/archives/${id}.${suffix}`;
      if (declaration.path !== expectedPath) fail(`${id} ${kind} has an undeclared path`);
      requiredSha256(declaration.sha256, `${id} ${kind}.sha256`);
      if (!Number.isInteger(declaration.bytes) || declaration.bytes <= 0 || declaration.bytes > MAX_ASSET_BYTES) fail(`${id} ${kind} has an invalid size`);
      const file = path.join(ROOT, declaration.path);
      const info = await assertedRegularFile(file, path.join(ROOT, "scenarios"));
      const body = await readFile(file);
      if (info.size !== declaration.bytes || sha256(body) !== declaration.sha256) fail(`${id} ${kind} hash or size mismatch`);
      if (kind === "frame_archive") {
        const entries = parseFrameTar(body, `${id} frame archive`);
        if (entries.size !== 150) fail(`${id} frame archive must contain 150 frames`);
        for (const [name, frame] of entries) frames.set(`${id}/${name}`, frame);
      }
    }
  }
  if (frames.size !== expectedHashes.size || [...expectedHashes.keys()].some((name) => !frames.has(name))) {
    fail("real-video archives do not match the canonical frame manifest");
  }
  return frames;
}

function annotationPolicy(real) {
  if (!real) {
    return {
      scope: "exhaustive",
      disclosure: "Synthetic annotations are exhaustive. An unannotated synthetic object is not implied.",
    };
  }
  return {
    scope: "exhaustive_full_frame_moving_objects",
    disclosure: "Real-video annotations exhaustively cover supported visible movers across the full image with stable physical IDs.",
  };
}

function scoreableRegion(real, manifest, frames) {
  return real
    ? { type: "full_frame", bounds: [0, 0, frames[0].width, frames[0].height] }
    : {
        type: manifest.scoreable_roi ? "fixed_roi" : "full_frame",
        bounds: manifest.scoreable_roi || [0, 0, frames[0].width, frames[0].height],
      };
}

function scoringPolicy(real) {
  if (real) {
    return {
      class_aware: true,
      scoreable_region: "full_frame",
      ignore_matching: "not_used",
      duplicate_predictions_penalized: true,
      background_predictions_penalized: true,
      temporal_metrics: ["HOTA", "IDF1", "ID switches", "fragmentation", "track completeness"],
    };
  }
  return {
    class_aware: true,
    target_matching_precedes_ignore_matching: true,
    ordinary_ignore_match: "IoU >= 0.5",
    ignore_region_match: ">= 50% prediction-area coverage",
    duplicate_target_predictions_penalized: true,
    background_hallucinations_inside_roi_penalized: true,
    outside_fixed_roi: "not_applicable_full_frame",
  };
}

async function publishJson(output, relative, value, maximum) {
  const body = Buffer.from(canonicalJson(value));
  if (body.length > maximum) fail(`${relative} is ${body.length} bytes; limit is ${maximum}`);
  if (output) {
    const destination = path.join(output, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, body);
  }
  return { bytes: body.length, sha256: sha256(body), url: `/${relative}` };
}

async function publishContentAddressedJson(output, kind, value) {
  const body = Buffer.from(canonicalJson(value));
  const digest = sha256(body);
  const relative = `scenario-catalog/v1/${kind}/sha256/${digest}.json`;
  if (output) {
    const destination = path.join(output, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, body);
  }
  return { bytes: body.length, sha256: digest, url: `/${relative}` };
}

async function publishFrame(output, source, expectedDigest, publishedFrames, label = source) {
  const buffer = Buffer.isBuffer(source) ? source : await readFile(source);
  const digest = sha256(buffer);
  if (expectedDigest && digest !== expectedDigest) fail(`frame hash mismatch for ${label}: expected ${expectedDigest}, got ${digest}`);
  if (buffer.length > MAX_ASSET_BYTES) fail(`frame exceeds Cloudflare's 25 MiB asset limit: ${label}`);
  if (buffer.length > RECOMMENDED_FRAME_BYTES) fail(`frame exceeds the 2 MiB catalog recommendation: ${label}`);
  const relative = `scenario-catalog/v1/assets/sha256/${digest}.jpg`;
  if (!publishedFrames.has(digest)) {
    if (output) {
      const destination = path.join(output, relative);
      await mkdir(path.dirname(destination), { recursive: true });
      await writeFile(destination, buffer);
    }
    publishedFrames.add(digest);
  }
  return { bytes: buffer.length, sha256: digest, url: `/${relative}`, dimensions: jpegDimensions(buffer) };
}

function frameInterval(frames) {
  if (frames.length < 2) return { fps: null, interval_ns: null };
  const intervals = frames.slice(1).map((frame, index) => frame.source_timestamp_ns - frames[index].source_timestamp_ns);
  if (!intervals.every((value) => value > 0)) fail("frame timestamps must be strictly increasing");
  const duration = frames.at(-1).source_timestamp_ns - frames[0].source_timestamp_ns;
  return {
    fps: Number((((frames.length - 1) * 1_000_000_000) / duration).toFixed(6)),
    interval_ns: intervals.every((value) => value === intervals[0]) ? intervals[0] : null,
  };
}

function countTargets(annotations) {
  return new Set(annotations.filter((row) => !row.ignore).map((row) => row.target_id)).size;
}

function sanitizeMetrics(metrics, label) {
  allowedObject(metrics, METRIC_FIELDS, label);
  if (!("observed_coverage" in metrics)) fail(`${label} is missing observed_coverage`);
  for (const [key, value] of Object.entries(metrics)) {
    if (key === "observed_coverage" && typeof value === "string") {
      if (value !== "not applicable; no targets by design") fail(`${label}.${key} has an undeclared string value`);
    } else if (key === "track_id_exhaustion_detected") {
      if (typeof value !== "boolean") fail(`${label}.${key} must be boolean`);
    } else {
      finiteNumber(value, `${label}.${key}`);
      if (value < 0) fail(`${label}.${key} cannot be negative`);
    }
  }
  return Object.fromEntries(Object.keys(metrics).sort().map((key) => [key, metrics[key]]));
}

async function loadBaselineEvidence(indexFile, catalogRoot, expectedIds) {
  await assertedRegularFile(indexFile, catalogRoot);
  const index = JSON.parse(await readFile(indexFile, "utf8"));
  allowedObject(index, new Set(["evidence_sources", "scenarios", "schema_version"]), "baseline evidence index");
  if (index.schema_version !== "cvbench.scenario-baseline-index/v1") fail("invalid baseline evidence index schema");
  plainObject(index.evidence_sources, "baseline evidence index.evidence_sources");
  plainObject(index.scenarios, "baseline evidence index.scenarios");
  if (JSON.stringify(Object.keys(index.scenarios).sort()) !== JSON.stringify([...expectedIds].sort())) {
    fail("baseline evidence must match the benchmark scenario union exactly");
  }
  const sources = new Map();
  for (const [evidenceId, declaration] of Object.entries(index.evidence_sources)) {
    requiredString(evidenceId, "baseline evidence id");
    allowedObject(declaration, new Set(["path", "sha256"]), `baseline evidence source ${evidenceId}`);
    requiredString(declaration.path, `baseline evidence source ${evidenceId}.path`);
    requiredSha256(declaration.sha256, `baseline evidence source ${evidenceId}.sha256`);
    if (!/^evidence\/[a-z0-9][a-z0-9-]*\.json$/.test(declaration.path)) fail(`baseline evidence source ${evidenceId} has an undeclared path`);
    const file = path.resolve(catalogRoot, declaration.path);
    await assertedRegularFile(file, catalogRoot);
    const body = await readFile(file);
    const digest = sha256(body);
    if (digest !== declaration.sha256) fail(`baseline evidence hash mismatch for ${evidenceId}: expected ${declaration.sha256}, got ${digest}`);
    const evidence = JSON.parse(body.toString("utf8"));
    allowedObject(evidence, new Set(["benchmark_id", "id", "report_sha256", "run_id", "scenarios", "schema_version", "status", "system", "validation_status"]), `baseline evidence ${evidenceId}`);
    if (evidence.schema_version !== "cvbench.sanitized-baseline-evidence/v1") fail(`invalid baseline evidence schema for ${evidenceId}`);
    if (evidence.id !== evidenceId) fail(`baseline evidence id mismatch for ${evidenceId}`);
    if (evidence.status !== "public" || evidence.validation_status !== "completed") fail(`baseline evidence ${evidenceId} is not completed public evidence`);
    requiredString(evidence.benchmark_id, `baseline evidence ${evidenceId}.benchmark_id`);
    requiredString(evidence.run_id, `baseline evidence ${evidenceId}.run_id`);
    requiredSha256(evidence.report_sha256, `baseline evidence ${evidenceId}.report_sha256`);
    const system = allowedObject(evidence.system, new Set(["id", "name", "sha256", "version"]), `baseline evidence ${evidenceId}.system`);
    requiredString(system.id, `baseline evidence ${evidenceId}.system.id`);
    requiredString(system.name, `baseline evidence ${evidenceId}.system.name`);
    requiredString(system.version, `baseline evidence ${evidenceId}.system.version`);
    requiredSha256(system.sha256, `baseline evidence ${evidenceId}.system.sha256`);
    plainObject(evidence.scenarios, `baseline evidence ${evidenceId}.scenarios`);
    const scenarios = Object.fromEntries(Object.entries(evidence.scenarios).map(([scenarioId, metrics]) => [
      scenarioId,
      sanitizeMetrics(metrics, `baseline evidence ${evidenceId}.scenarios.${scenarioId}`),
    ]));
    sources.set(evidenceId, {
      benchmark_id: evidence.benchmark_id,
      evidence_id: evidenceId,
      evidence_sha256: digest,
      report_sha256: evidence.report_sha256,
      run_id: evidence.run_id,
      scenarios,
      system: { ...system },
      validation_status: evidence.validation_status,
    });
  }
  const result = {};
  const usedScenarios = new Map([...sources.keys()].map((id) => [id, []]));
  for (const id of expectedIds) {
    const assignment = allowedObject(index.scenarios[id], new Set(["evidence_id"]), `baseline evidence assignment ${id}`);
    const evidence = sources.get(requiredString(assignment.evidence_id, `baseline evidence assignment ${id}.evidence_id`));
    if (!evidence) fail(`baseline evidence assignment ${id} references an undeclared source`);
    const metrics = evidence.scenarios[id];
    if (!metrics) fail(`baseline evidence ${evidence.evidence_id} has no summary for ${id}`);
    usedScenarios.get(evidence.evidence_id).push(id);
    result[id] = {
      status: "public",
      system_id: evidence.system.id,
      system_name: evidence.system.name,
      system_version: evidence.system.version,
      system_sha256: evidence.system.sha256,
      validation_status: evidence.validation_status,
      benchmark_id: evidence.benchmark_id,
      run_id: evidence.run_id,
      report_sha256: evidence.report_sha256,
      source_evidence: {
        id: evidence.evidence_id,
        sha256: evidence.evidence_sha256,
        schema_version: "cvbench.sanitized-baseline-evidence/v1",
      },
      metrics,
    };
  }
  for (const [evidenceId, evidence] of sources) {
    const declared = Object.keys(evidence.scenarios).sort();
    const used = usedScenarios.get(evidenceId).sort();
    if (JSON.stringify(declared) !== JSON.stringify(used)) fail(`baseline evidence ${evidenceId} contains undeclared or unused scenario summaries`);
  }
  return result;
}

async function scenarioDocument({ id, manifestPath, membership, metadata, baseline, expectedHashes, realFrames, output, publishedFrames }) {
  const manifest = parseYaml(await readFile(manifestPath, "utf8"));
  allowedObject(manifest, new Set(["annotation_scope", "family", "faults", "frames", "ground_truth", "id", "license", "ontology", "schema_version", "scoreable_roi", "sequence_id", "source"]), `${id} scenario manifest`);
  if (manifest.schema_version !== "cvbench.scenario/v1" || manifest.id !== id) fail(`invalid scenario manifest for ${id}`);
  requiredString(manifest.family, `${id} scenario manifest.family`);
  requiredString(manifest.sequence_id, `${id} scenario manifest.sequence_id`);
  requiredString(manifest.license, `${id} scenario manifest.license`);
  requiredString(manifest.source, `${id} scenario manifest.source`);
  requiredString(manifest.ground_truth, `${id} scenario manifest.ground_truth`);
  if (!Array.isArray(manifest.frames) || !manifest.frames.length) fail(`${id} scenario manifest requires frames`);
  manifest.frames.forEach((frame, index) => {
    allowedObject(frame, new Set(["frame_index", "height", "path", "source_timestamp_ns", "width"]), `${id} frame ${index}`);
    requiredString(frame.path, `${id} frame ${index}.path`);
  });
  const real = id.startsWith("rvmot-");
  if (real) {
    if (manifest.annotation_scope !== "exhaustive_full_frame_moving_objects") fail(`${id} must declare exhaustive full-frame annotations`);
    if ("scoreable_roi" in manifest) fail(`${id} must not declare a scoreable ROI`);
    if (JSON.stringify(manifest.ontology) !== JSON.stringify(["person", "vehicle", "dog"])) fail(`${id} has an invalid ontology`);
    manifest.frames.forEach((frame, index) => {
      if (frame.source_timestamp_ns !== Math.round(index * 1_000_000_000 / 30)) fail(`${id} must preserve exact 30 FPS timestamps`);
    });
  }
  const source = metadata.sources[real ? id : "synthetic"];
  if (!source || source.license !== manifest.license) fail(`license metadata mismatch for ${id}`);
  const scenarioMeta = metadata.scenarios[id];
  if (!scenarioMeta || !Array.isArray(scenarioMeta.failure_modes) || !scenarioMeta.failure_modes.length) fail(`missing curated metadata for ${id}`);
  if (!baseline) fail(`missing allowlisted baseline evidence for ${id}`);

  const manifestDirectory = path.dirname(manifestPath);
  const localGroundTruth = path.join(manifestDirectory, "ground_truth.jsonl");
  await assertedRegularFile(localGroundTruth, path.join(ROOT, "scenarios"));
  const annotations = (await readFile(localGroundTruth, "utf8")).split("\n").filter(Boolean).map((line, index) => {
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch {
      fail(`malformed JSONL at ${path.relative(ROOT, localGroundTruth)}:${index + 1}`);
    }
    return sanitizeAnnotation(parsed, manifest, index);
  });
  const frameByTimestamp = new Map(manifest.frames.map((frame) => [frame.source_timestamp_ns, frame]));
  const annotationsByTimestamp = new Map(manifest.frames.map((frame) => [frame.source_timestamp_ns, []]));
  for (const [index, row] of annotations.entries()) {
    const frame = frameByTimestamp.get(row.source_timestamp_ns);
    if (!frame) fail(`${id} annotation ${index} references an undeclared timestamp`);
    if (row.sequence_id !== manifest.sequence_id) fail(`${id} annotation ${index} has the wrong sequence_id`);
    annotationsByTimestamp.get(row.source_timestamp_ns).push(row);
  }
  if (manifest.scoreable_roi) validateBox(manifest.scoreable_roi, manifest.frames[0].width, manifest.frames[0].height, `${id} scoreable ROI`);
  const faults = (manifest.faults || []).map((fault, index) => sanitizeFault(fault, id, index));

  const frames = [];
  for (const frame of manifest.frames) {
    if (!Number.isInteger(frame.frame_index) || frame.frame_index !== frames.length) fail(`${id} frame indexes are not contiguous`);
    if (!Number.isInteger(frame.source_timestamp_ns) || frame.source_timestamp_ns < 0) fail(`${id} has an invalid frame timestamp`);
    if (!Number.isInteger(frame.width) || !Number.isInteger(frame.height)) fail(`${id} has invalid frame dimensions`);
    let sourcePath;
    let expectedDigest;
    if (real) {
      const name = `frame-${String(frame.frame_index).padStart(4, "0")}.jpg`;
      const relative = `${id}/frames/${name}`;
      sourcePath = realFrames.get(relative);
      expectedDigest = expectedHashes.get(relative);
      if (!expectedDigest) fail(`no canonical hash declared for ${id}/${name}`);
      if (!sourcePath) fail(`no archived frame declared for ${id}/${name}`);
    } else {
      sourcePath = path.resolve(manifestDirectory, frame.path);
      await assertedRegularFile(sourcePath, path.join(ROOT, "scenarios"));
    }
    const published = await publishFrame(output, sourcePath, expectedDigest, publishedFrames, `${id}/frame-${frame.frame_index}`);
    if (published.dimensions.width !== frame.width || published.dimensions.height !== frame.height) {
      fail(`${id} frame ${frame.frame_index} dimensions do not match the manifest`);
    }
    frames.push({
      frame_index: frame.frame_index,
      source_timestamp_ns: frame.source_timestamp_ns,
      width: frame.width,
      height: frame.height,
      media: { url: published.url, sha256: published.sha256, bytes: published.bytes, media_type: "image/jpeg" },
    });
  }
  const groupedAnnotations = frames.map((frame) => ({
    frame_index: frame.frame_index,
    source_timestamp_ns: frame.source_timestamp_ns,
    objects: annotationsByTimestamp.get(frame.source_timestamp_ns),
  }));
  const frameManifest = await publishContentAddressedJson(output, "frame-manifests", {
    schema_version: "cvbench.frame-manifest/v1",
    scenario_id: id,
    exact_benchmark_sequence: true,
    frames,
  });
  const annotationManifest = await publishContentAddressedJson(output, "annotation-manifests", {
    schema_version: "cvbench.annotation-manifest/v1",
    scenario_id: id,
    annotation_policy: annotationPolicy(real),
    scoreable_region: scoreableRegion(real, manifest, frames),
    faults,
    frames: groupedAnnotations,
  });
  const baselineManifest = await publishContentAddressedJson(output, "baseline-manifests", {
    schema_version: "cvbench.scenario-baseline/v1",
    scenario_id: id,
    ...baseline,
  });
  const timing = frameInterval(frames);
  const classes = [...new Set(annotations.filter((row) => !row.ignore).map((row) => row.class_id))].sort();
  return {
    schema_version: "cvbench.public-scenario/v1",
    id,
    stable_id: id,
    sequence_id: manifest.sequence_id,
    version: real ? "2.0.0" : "1.0.0",
    pack: { id: real ? "real-video-v2" : "synthetic-v1", version: real ? "2.0.0" : "1.0.0", status: "public" },
    status: "public",
    title: scenarioMeta.title,
    description: scenarioMeta.description,
    task: "online class-aware multi-object tracking",
    failure_modes: scenarioMeta.failure_modes,
    benchmark_membership: [...membership].sort(),
    media: {
      exact_benchmark_frame_sequence: true,
      frame_count: frames.length,
      duration_ns: frames.at(-1).source_timestamp_ns - frames[0].source_timestamp_ns,
      fps: timing.fps,
      frame_interval_ns: timing.interval_ns,
      width: frames[0].width,
      height: frames[0].height,
      frame_manifest: frameManifest,
    },
    annotations: {
      annotation_manifest: annotationManifest,
      policy: annotationPolicy(real),
      scoreable_region: scoreableRegion(real, manifest, frames),
      target_count: countTargets(annotations),
      class_ids: classes,
      object_rows: annotations.filter((row) => !row.ignore).length,
      ignore_rows: annotations.filter((row) => row.ignore).length,
      scoring: scoringPolicy(real),
    },
    provenance: {
      source: Object.fromEntries(Object.keys(source).sort().map((field) => [field, source[field]])),
      author: real ? `${source.creator}; annotations and preparation by CVBench contributors` : source.creator,
      preparation: real ? {
        base_image: metadata.preparation.base_image,
        dockerfile_sha256: PREPARATION_HASH,
        expected_frame_manifest: metadata.preparation.expected_frame_manifest,
        identity: metadata.preparation.identity,
        platform: metadata.preparation.platform,
        toolchain: metadata.preparation.toolchain,
      } : {
        base_image: "not applicable; deterministic source generator",
        dockerfile_sha256: sha256(Buffer.from(canonicalJson(manifest))),
        expected_frame_manifest: path.relative(ROOT, manifestPath).replaceAll(path.sep, "/"),
        identity: "cvbench-synthetic-generator/v1",
        platform: "portable deterministic Python generation",
        toolchain: "src/cvbench/synthetic.py",
      },
    },
    baseline: {
      manifest: baselineManifest,
      system_id: baseline.system_id,
      system_version: baseline.system_version,
      validation_status: baseline.validation_status,
    },
    public_data_disclosure: "All current CVBench scenarios, media, and annotations are public and may be tuned to or memorized. Runtime isolation prevents future-frame and host-data access during execution; it does not make public benchmark data secret. Future hidden challenges are outside catalog v1.",
  };
}

async function assertStaticAllowlist() {
  const actual = [];
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) fail(`symlink in public source: ${path.relative(PUBLIC, file)}`);
      if (entry.isDirectory()) await walk(file);
      else if (entry.isFile()) actual.push(path.relative(PUBLIC, file).replaceAll(path.sep, "/"));
      else fail(`non-regular public source: ${path.relative(PUBLIC, file)}`);
    }
  }
  await walk(PUBLIC);
  actual.sort();
  const expected = [...STATIC_FILES].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) fail(`public source allowlist mismatch: ${actual.join(", ")}`);
}

async function outputEvidence(output) {
  const files = [];
  let total = 0;
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      if (entry.isDirectory()) await walk(file);
      else {
        const relative = path.relative(output, file).replaceAll(path.sep, "/");
        const extension = path.extname(relative);
        if (!ALLOWED_PUBLISHED_EXTENSIONS.has(extension)) fail(`undeclared published extension: ${relative}`);
        if (PRIVATE_PATH_PATTERN.test(relative)) fail(`private artifact path in output: ${relative}`);
        const body = await readFile(file);
        if (body.length > MAX_ASSET_BYTES) fail(`published file exceeds 25 MiB: ${relative}`);
        if (path.extname(relative) !== ".jpg") {
          const text = body.toString("utf8");
          for (const pattern of PRIVATE_CONTENT_PATTERNS) {
            if (pattern.test(text)) fail(`private artifact content in output: ${relative}`);
          }
        }
        total += body.length;
        files.push({ path: relative, bytes: body.length, sha256: sha256(body) });
      }
    }
  }
  await walk(output);
  if (total > MAX_SITE_BYTES) fail(`published site is ${total} bytes; initial catalog limit is ${MAX_SITE_BYTES}`);
  return { files: files.sort((a, b) => a.path.localeCompare(b.path)), total_bytes: total };
}

async function buildCatalog(output, { metadataSource } = {}) {
  assertSafeOutput(output);
  try {
    const outputInfo = await lstat(output);
    if (outputInfo.isSymbolicLink()) fail("output directory cannot be a symlink");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const { benchmarkMembership, scenarioPaths } = await benchmarkInventory();
  const ids = [...scenarioPaths.keys()].sort();
  const metadata = sanitizeMetadata(metadataSource ?? await readYaml("scenario-catalog/metadata.yaml"), ids);
  const baselines = await loadBaselineEvidence(path.join(CATALOG_SOURCE, "baselines.json"), CATALOG_SOURCE, ids);
  const expectedHashes = await expectedRealHashes();
  const realFrames = await loadRealFrameArchives(expectedHashes);
  await assertStaticAllowlist();

  // Validate every declared record, path, hash, geometry, and size before replacing output.
  const preflightFrames = new Set();
  for (const id of ids) {
    await scenarioDocument({
      id,
      manifestPath: scenarioPaths.get(id),
      membership: benchmarkMembership.get(id),
      metadata,
      baseline: baselines[id],
      expectedHashes,
      realFrames,
      output: null,
      publishedFrames: preflightFrames,
    });
  }

  await rm(output, { recursive: true, force: true });
  await mkdir(output, { recursive: true });
  for (const relative of STATIC_FILES) {
    const source = path.join(PUBLIC, relative);
    await assertedRegularFile(source, PUBLIC);
    const destination = path.join(output, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    await cp(source, destination);
  }

  const publishedFrames = new Set();
  const summaries = [];
  for (const id of ids) {
    const document = await scenarioDocument({
      id,
      manifestPath: scenarioPaths.get(id),
      membership: benchmarkMembership.get(id),
      metadata,
      baseline: baselines[id],
      expectedHashes,
      realFrames,
      output,
      publishedFrames,
    });
    const detail = await publishJson(output, `scenario-catalog/v1/scenarios/${id}.json`, document, MAX_SCENARIO_BYTES);
    summaries.push({
      id,
      title: document.title,
      description: document.description,
      pack: document.pack,
      failure_modes: document.failure_modes,
      frames: document.media.frame_count,
      fps: document.media.fps,
      resolution: `${document.media.width}x${document.media.height}`,
      annotation_scope: document.annotations.policy.scope,
      license: document.provenance.source.license,
      detail: { url: detail.url, sha256: detail.sha256, bytes: detail.bytes },
    });
  }
  const catalog = {
    schema_version: "cvbench.scenario-catalog/v1",
    id: "cvbench-current-public-scenarios",
    version: metadata.catalog.version,
    status: "public",
    generated_from: { benchmark_manifests: BENCHMARK_FILES, derivation: "exact set union" },
    scenario_count: summaries.length,
    all_current_scenarios_public: true,
    disclosure: "All current scenarios, exact media, and full annotations are public and may be tuned to or memorized. Runtime isolation is an execution boundary, not a secrecy claim. Future hidden challenges are out of scope.",
    scenarios: summaries,
  };
  const catalogOutput = await publishJson(output, "scenario-catalog/v1/catalog.json", catalog, MAX_CATALOG_BYTES);
  await publishJson(output, ".well-known/cvbench-scenarios.json", {
    schema_version: "cvbench.scenario-discovery/v1",
    catalog_url: catalogOutput.url,
    catalog_sha256: catalogOutput.sha256,
    catalog_version: catalog.version,
    scenario_count: catalog.scenario_count,
    all_current_scenarios_public: true,
  }, MAX_CATALOG_BYTES);
  const evidence = await outputEvidence(output);
  await publishJson(output, "scenario-catalog/v1/build-evidence.json", {
    schema_version: "cvbench.scenario-catalog-build/v1",
    scenario_count: summaries.length,
    unique_frame_assets: publishedFrames.size,
    total_bytes_before_evidence: evidence.total_bytes,
    files: evidence.files,
  }, MAX_ASSET_BYTES);
  const final = await outputEvidence(output);
  return { output, scenarios: summaries.length, unique_frames: publishedFrames.size, files: final.files.length, bytes: final.total_bytes };
}

async function main() {
  const outputArgument = process.argv.indexOf("--output");
  if (outputArgument >= 0 && !process.argv[outputArgument + 1]) fail("--output requires a directory");
  const output = outputArgument >= 0 ? path.resolve(process.cwd(), process.argv[outputArgument + 1]) : DEFAULT_OUTPUT;
  process.stdout.write(`${JSON.stringify(await buildCatalog(output))}\n`);
}

const invokedAsScript = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedAsScript) await main();

export {
  allowedObject,
  assertSafeOutput,
  assertedRegularFile,
  buildCatalog,
  jpegDimensions,
  loadBaselineEvidence,
  outputEvidence,
  publishFrame,
  sanitizeAnnotation,
  sanitizeFault,
  validateBox,
};
