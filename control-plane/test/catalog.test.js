import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { parse as parseYaml } from "yaml";

import {
  allowedObject,
  assertSafeOutput,
  assertedRegularFile,
  buildCatalog,
  loadBaselineEvidence,
  outputEvidence,
  publishFrame,
  sanitizeAnnotation,
  sanitizeFault,
  validateBox,
} from "../scripts/build-scenario-catalog.mjs";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = path.resolve(CONTROL_PLANE, "..");

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function filesBelow(root) {
  const files = [];
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) await walk(absolute);
      else files.push(path.relative(root, absolute).replaceAll(path.sep, "/"));
    }
  }
  await walk(root);
  return files.sort();
}

function build(output) {
  const result = runBuild(output);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  return JSON.parse(result.stdout.trim());
}

function runBuild(output) {
  return spawnSync(process.execPath, ["scripts/build-scenario-catalog.mjs", "--output", output], {
    cwd: CONTROL_PLANE,
    encoding: "utf8",
  });
}

test("two clean catalog builds are byte-identical and within budgets", async (context) => {
  const packageJson = JSON.parse(await readFile(path.join(CONTROL_PLANE, "package.json"), "utf8"));
  assert.equal(packageJson.scripts.postinstall, "npm run build");
  const first = path.join(CONTROL_PLANE, "dist-test-a");
  const second = path.join(CONTROL_PLANE, "dist-test-b");
  context.after(async () => Promise.all([rm(first, { recursive: true, force: true }), rm(second, { recursive: true, force: true })]));
  const firstSummary = build("dist-test-a");
  const secondSummary = build("dist-test-b");
  assert.deepEqual({ ...firstSummary, output: null }, { ...secondSummary, output: null });
  assert.equal(firstSummary.scenarios, 16);
  assert.ok(firstSummary.bytes < 50 * 1024 * 1024);
  const firstFiles = await filesBelow(first);
  const secondFiles = await filesBelow(second);
  assert.deepEqual(firstFiles, secondFiles);
  for (const relative of firstFiles) {
    const [left, right] = await Promise.all([readFile(path.join(first, relative)), readFile(path.join(second, relative))]);
    assert.equal(sha256(left), sha256(right), relative);
    assert.ok(left.length < 25 * 1024 * 1024, relative);
  }
});

test("catalog equals the benchmark union and every published hash verifies", async (context) => {
  const output = path.join(CONTROL_PLANE, "dist-test-catalog");
  context.after(async () => rm(output, { recursive: true, force: true }));
  build("dist-test-catalog");
  const catalogBody = await readFile(path.join(output, "scenario-catalog/v1/catalog.json"));
  const catalog = JSON.parse(catalogBody);
  assert.ok(catalogBody.length < 256 * 1024);
  assert.equal(catalog.scenario_count, 16);
  assert.equal(catalog.all_current_scenarios_public, true);

  const expected = new Set();
  for (const filename of ["long-running-stability.yaml", "persistent-target-tracking.yaml", "public-whole-system-v2.yaml", "real-video-v2.yaml"]) {
    const benchmarkPath = path.join(ROOT, "benchmarks", filename);
    const benchmark = parseYaml(await readFile(benchmarkPath, "utf8"));
    for (const declared of benchmark.scenarios) {
      const manifest = parseYaml(await readFile(path.resolve(path.dirname(benchmarkPath), declared), "utf8"));
      expected.add(manifest.id);
    }
  }
  assert.deepEqual(catalog.scenarios.map(({ id }) => id).sort(), [...expected].sort());

  for (const summary of catalog.scenarios) {
    assert.equal(summary.pack.status, "public");
    const detailBody = await readFile(path.join(output, summary.detail.url));
    assert.equal(sha256(detailBody), summary.detail.sha256);
    assert.ok(detailBody.length < 512 * 1024);
    const detail = JSON.parse(detailBody);
    assert.equal(detail.id, summary.id);
    assert.equal(detail.status, "public");
    for (const reference of [detail.media.frame_manifest, detail.annotations.annotation_manifest, detail.baseline.manifest]) {
      const body = await readFile(path.join(output, reference.url));
      assert.equal(sha256(body), reference.sha256);
    }
    const frameManifest = JSON.parse(await readFile(path.join(output, detail.media.frame_manifest.url)));
    const annotationManifest = JSON.parse(await readFile(path.join(output, detail.annotations.annotation_manifest.url)));
    assert.equal(frameManifest.frames.length, summary.frames);
    assert.equal(annotationManifest.frames.length, summary.frames);
    for (const frame of frameManifest.frames) {
      const media = await readFile(path.join(output, frame.media.url));
      assert.equal(sha256(media), frame.media.sha256);
      assert.ok(media.length <= 2 * 1024 * 1024);
    }
  }

  const evidence = JSON.parse(await readFile(path.join(output, "scenario-catalog/v1/build-evidence.json")));
  for (const item of evidence.files) {
    const body = await readFile(path.join(output, item.path));
    assert.equal(body.length, item.bytes, item.path);
    assert.equal(sha256(body), item.sha256, item.path);
  }
});

test("licenses, annotation scope, scoring rules, and track churn evidence are explicit", async (context) => {
  const output = path.join(CONTROL_PLANE, "dist-test-policy");
  context.after(async () => rm(output, { recursive: true, force: true }));
  build("dist-test-policy");
  const detail = async (id) => JSON.parse(await readFile(path.join(output, `scenario-catalog/v1/scenarios/${id}.json`)));
  for (const id of ["rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6"]) {
    const scenario = await detail(id);
    assert.equal(scenario.provenance.source.license, "CC-BY-4.0");
    assert.match(scenario.provenance.source.attribution, /licensed CC BY 4\.0/);
    assert.match(scenario.provenance.source.transformation, /consecutive frames/);
    assert.match(scenario.provenance.source.annotation_provenance, /Per-frame MEVA geometry and types/);
    assert.match(scenario.provenance.source.corrections, /no temporal interpolation/);
    assert.equal(scenario.annotations.policy.scope, "exhaustive_full_frame_moving_objects");
    assert.equal(scenario.annotations.scoring.scoreable_region, "full_frame");
    assert.equal(scenario.annotations.ignore_rows, 0);
    assert.deepEqual(scenario.annotations.scoreable_region.bounds, [0, 0, scenario.media.width, scenario.media.height]);
    assert.equal(scenario.media.fps, 30);
    const baseline = JSON.parse(await readFile(path.join(output, scenario.baseline.manifest.url)));
    assert.ok("hota" in baseline.metrics);
    assert.ok("idf1" in baseline.metrics);
    assert.ok("association_accuracy" in baseline.metrics);
    assert.equal(baseline.metrics.neutral_ignored_predictions, 0);
  }
  const empty = await detail("synthetic-false-detection");
  assert.equal(empty.annotations.target_count, 0);
  assert.equal(empty.annotations.policy.scope, "exhaustive");
  const churn = await detail("synthetic-track-id-churn");
  const baseline = JSON.parse(await readFile(path.join(output, churn.baseline.manifest.url)));
  assert.equal(baseline.validation_status, "completed");
  assert.equal(baseline.metrics.track_id_reuse_events, 0);
  assert.equal(baseline.metrics.track_id_exhaustion_detected, false);
  assert.equal(baseline.metrics.state_contamination_events, 0);
  assert.equal(baseline.source_evidence.id, "track-id-churn");
  const evidenceSource = await readFile(path.join(ROOT, "scenario-catalog/evidence/track-id-churn.json"));
  assert.equal(baseline.source_evidence.sha256, sha256(evidenceSource));
  assert.equal(churn.annotations.scoring.target_matching_precedes_ignore_matching, true);
  assert.equal(churn.annotations.scoring.ignore_region_match, ">= 50% prediction-area coverage");
  assert.equal(churn.annotations.scoring.ordinary_ignore_match, "IoU >= 0.5");
});

test("catalog safety helpers reject traversal, symlinks, malformed geometry, hash mismatch, and private artifacts", async (context) => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "cvbench-catalog-test-"));
  context.after(async () => rm(temporary, { recursive: true, force: true }));
  const root = path.join(temporary, "root");
  await mkdir(root);
  const regular = path.join(root, "frame.jpg");
  const outside = path.join(temporary, "outside.jpg");
  await writeFile(regular, "regular");
  await writeFile(outside, "outside");
  await assert.rejects(assertedRegularFile(outside, root), /escapes allowlisted root/);
  const linked = path.join(root, "linked.jpg");
  await symlink(regular, linked);
  await assert.rejects(assertedRegularFile(linked, root), /symlink is not publishable/);
  const realDirectory = path.join(root, "real-directory");
  await mkdir(realDirectory);
  await writeFile(path.join(realDirectory, "nested.jpg"), "nested");
  const linkedDirectory = path.join(root, "linked-directory");
  await symlink(realDirectory, linkedDirectory);
  await assert.rejects(assertedRegularFile(path.join(linkedDirectory, "nested.jpg"), root), /symlink is not publishable/);
  assert.throws(() => validateBox([0, 0, 11, 10], 10, 10, "box"), /outside/);
  assert.throws(() => validateBox([0, Number.NaN, 5, 5], 10, 10, "box"), /finite/);
  const published = new Set();
  await assert.rejects(publishFrame(root, regular, "0".repeat(64), published), /frame hash mismatch/);
  const output = path.join(temporary, "output");
  await mkdir(output);
  await writeFile(path.join(output, "credentials.json"), "{}");
  await assert.rejects(outputEvidence(output), /private artifact path/);
  await rm(path.join(output, "credentials.json"));
  await writeFile(path.join(output, "safe-name.json"), '{"contact":"private@example.invalid"}');
  await assert.rejects(outputEvidence(output), /private artifact content/);

  const manifest = { id: "canary", sequence_id: "sequence", ontology: ["target"], frames: [{ source_timestamp_ns: 0, width: 10, height: 10 }] };
  const annotation = {
    bbox_xyxy: [0, 0, 5, 5],
    class_id: "target",
    eligible_for_detection: true,
    occlusion: "none",
    on_screen: true,
    sequence_id: "sequence",
    source_timestamp_ns: 0,
    target_id: "target-1",
    visibility_fraction: 1,
  };
  assert.throws(() => sanitizeAnnotation({ ...annotation, undeclared: "canary" }, manifest, 0), /undeclared field undeclared/);
  assert.throws(() => sanitizeAnnotation({ ...annotation, class_id: "unsupported-cat" }, manifest, 0), /outside the scenario ontology/);
  assert.throws(() => sanitizeAnnotation({ ...annotation, metadata: { api_key: "canary" } }, manifest, 0), /private-looking field api_key/);
  assert.throws(() => sanitizeAnnotation({ ...annotation, local_path: "/private/canary" }, manifest, 0), /private-looking field local_path/);
  assert.throws(() => sanitizeFault({ type: "blackout", api_key: "canary" }, "canary", 0), /private-looking field api_key/);
  assert.throws(() => allowedObject({ safe: { local_path: "canary" } }, new Set(["safe"]), "source record"), /private-looking field local_path/);
});

test("baseline evidence is hashed, allowlisted, and summary-bound", async (context) => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "cvbench-evidence-test-"));
  context.after(async () => rm(temporary, { recursive: true, force: true }));
  await mkdir(path.join(temporary, "evidence"));
  const evidence = {
    schema_version: "cvbench.sanitized-baseline-evidence/v1",
    id: "canary",
    status: "public",
    system: { id: "sut", name: "Sanitized SUT", version: "v1", sha256: "a".repeat(64) },
    validation_status: "completed",
    benchmark_id: "benchmark",
    run_id: "run",
    report_sha256: "b".repeat(64),
    scenarios: { scenario: { observed_coverage: 1, false_detections: 0 } },
  };
  const evidenceBody = Buffer.from(`${JSON.stringify(evidence)}\n`);
  await writeFile(path.join(temporary, "evidence/canary.json"), evidenceBody);
  const indexPath = path.join(temporary, "baselines.json");
  const writeIndex = async (digest) => writeFile(indexPath, JSON.stringify({
    schema_version: "cvbench.scenario-baseline-index/v1",
    evidence_sources: { canary: { path: "evidence/canary.json", sha256: digest } },
    scenarios: { scenario: { evidence_id: "canary" } },
  }));
  await writeIndex(sha256(evidenceBody));
  const loaded = await loadBaselineEvidence(indexPath, temporary, ["scenario"]);
  assert.equal(loaded.scenario.source_evidence.sha256, sha256(evidenceBody));
  assert.deepEqual(loaded.scenario.metrics, evidence.scenarios.scenario);
  await writeIndex("0".repeat(64));
  await assert.rejects(loadBaselineEvidence(indexPath, temporary, ["scenario"]), /baseline evidence hash mismatch/);
  evidence.system.api_key = "canary";
  const poisonedBody = Buffer.from(`${JSON.stringify(evidence)}\n`);
  await writeFile(path.join(temporary, "evidence/canary.json"), poisonedBody);
  await writeIndex(sha256(poisonedBody));
  await assert.rejects(loadBaselineEvidence(indexPath, temporary, ["scenario"]), /private-looking field api_key/);
});

test("destructive output targets fail before source markers are touched", async () => {
  const protectedTargets = [
    [ROOT, path.join(ROOT, "README.md")],
    [CONTROL_PLANE, path.join(CONTROL_PLANE, "package.json")],
    [path.join(CONTROL_PLANE, "public"), path.join(CONTROL_PLANE, "public/index.html")],
    [path.join(CONTROL_PLANE, "public/nested"), path.join(CONTROL_PLANE, "public/index.html")],
    [path.join(CONTROL_PLANE, "src"), path.join(CONTROL_PLANE, "src/app.js")],
    [path.join(CONTROL_PLANE, "scripts"), path.join(CONTROL_PLANE, "scripts/build-scenario-catalog.mjs")],
    [path.join(CONTROL_PLANE, "migrations"), path.join(CONTROL_PLANE, "migrations/0001_initial.sql")],
  ];
  const temporary = await mkdtemp(path.join(os.tmpdir(), "cvbench-output-canary-"));
  const outside = path.join(temporary, "outside");
  await mkdir(outside);
  const outsideMarker = path.join(outside, "marker.txt");
  await writeFile(outsideMarker, "survives");
  protectedTargets.push([outside, outsideMarker]);
  try {
    for (const [target, marker] of protectedTargets) {
      const before = await readFile(marker);
      const result = runBuild(target);
      assert.notEqual(result.status, 0, target);
      assert.match(result.stderr, /output must be/);
      assert.deepEqual(await readFile(marker), before, target);
    }
    assert.doesNotThrow(() => assertSafeOutput(path.join(CONTROL_PLANE, "dist")));
    assert.doesNotThrow(() => assertSafeOutput(path.join(CONTROL_PLANE, "dist-test-safe")));
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("unknown and private-looking source fields fail preflight before output replacement", async (context) => {
  const output = path.join(CONTROL_PLANE, "dist-test-preflight-canary");
  context.after(async () => rm(output, { recursive: true, force: true }));
  await mkdir(output);
  const marker = path.join(output, "marker.txt");
  await writeFile(marker, "preflight marker");
  const metadata = parseYaml(await readFile(path.join(ROOT, "scenario-catalog/metadata.yaml"), "utf8"));
  metadata.scenarios["synthetic-acquisition"].api_key = "must-never-publish";
  await assert.rejects(buildCatalog(output, { metadataSource: metadata }), /private-looking field api_key/);
  assert.equal(await readFile(marker, "utf8"), "preflight marker");
  delete metadata.scenarios["synthetic-acquisition"].api_key;
  metadata.scenarios["synthetic-acquisition"].undeclared = "must-never-publish";
  await assert.rejects(buildCatalog(output, { metadataSource: metadata }), /undeclared field undeclared/);
  assert.equal(await readFile(marker, "utf8"), "preflight marker");
});

test("public surfaces use safe DOM rendering, strict CSP, and corrected system terminology", async () => {
  const javascript = await Promise.all(["app.js", "operator.js", "scenario-app.js"].map((name) => readFile(path.join(CONTROL_PLANE, "public", name), "utf8")));
  for (const source of javascript) assert.doesNotMatch(source, /\.innerHTML\s*=/i);
  assert.match(javascript[0], /scores\.replay_profile != null && scores\.replay_rate != null/);
  const headers = await readFile(path.join(CONTROL_PLANE, "public/_headers"), "utf8");
  assert.match(headers, /object-src 'none'/);
  assert.match(headers, /media-src 'self'/);
  assert.match(headers, /max-age=31556952, immutable/);

  const currentSurfaces = [
    "README.md",
    "docs/capability-matrix.md",
    "docs/control-plane.md",
    "docs/operator-audit.md",
    "docs/scenario-catalog.md",
    "control-plane/public/index.html",
    "control-plane/public/operator.html",
    "control-plane/public/scenarios/index.html",
    "control-plane/public/app.js",
    "control-plane/public/operator.js",
    "control-plane/public/scenario-app.js",
    "control-plane/src/app.js",
    "src/cvbench/audit.py",
  ];
  const banned = [
    /the system is the model/i,
    /submit a model/i,
    /submit your model/i,
    /model containers?/i,
    /model-submission interface/i,
    /annotation paths to the model/i,
  ];
  for (const relative of currentSurfaces) {
    const source = await readFile(path.join(ROOT, relative), "utf8");
    for (const pattern of banned) assert.doesNotMatch(source, pattern, relative);
  }
  const home = await readFile(path.join(CONTROL_PLANE, "public/index.html"), "utf8");
  assert.match(home, /Benchmark the whole<br>vision system\./);
  assert.match(home, /Submit a system image\./);
});
