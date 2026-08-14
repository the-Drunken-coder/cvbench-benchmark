#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { parse as parseYaml } from "yaml";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUTPUT = path.join(CONTROL_PLANE, "public/dataset-catalog/v1/catalog.json");
const PREVIEWS = path.join(CONTROL_PLANE, "public/dataset-catalog/v1/previews");
const REPOSITORY_URL = "https://github.com/the-Drunken-coder/cvbench-dataset";
const repository = process.argv[2] && path.resolve(process.cwd(), process.argv[2]);

if (!repository) throw new Error("usage: npm run sync:datasets -- /path/to/cvbench-dataset");

function required(value, label) {
  if (value === undefined || value === null || value === "") throw new Error(`${label} is required`);
  return value;
}

function revision() {
  const status = spawnSync("git", ["status", "--porcelain", "--untracked-files=all"], { cwd: repository, encoding: "utf8" });
  if (status.status !== 0) throw new Error("dataset repository must be a Git checkout");
  if (status.stdout.trim()) throw new Error("dataset repository must be clean before syncing");

  const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: repository, encoding: "utf8" });
  const commit = head.stdout?.trim();
  if (head.status !== 0 || !/^[a-f0-9]{40}$/.test(commit)) throw new Error("dataset repository must be at a full commit");
  return commit;
}

async function jsonLines(file) {
  return (await readFile(file, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function preview(id) {
  try {
    const candidates = (await readdir(PREVIEWS))
      .filter((filename) => filename.startsWith(`${id}.`) && filename.endsWith(".mp4"));
    if (candidates.length === 0) return null;
    if (candidates.length > 1) throw new Error(`${id} has multiple browser previews`);

    const [filename] = candidates;
    const body = await readFile(path.join(PREVIEWS, filename));
    const sha256 = createHash("sha256").update(body).digest("hex");
    if (filename !== `${id}.${sha256.slice(0, 12)}.mp4`) {
      throw new Error(`${id} preview filename does not match its content hash`);
    }
    return {
      url: `/dataset-catalog/v1/previews/${filename}`,
      sha256,
      bytes: body.length,
    };
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

function sha256(body) {
  return createHash("sha256").update(body).digest("hex");
}

function humanApprovals(reviews, clipId, artifacts) {
  return new Set(reviews
    .filter((review) => review.clip_id === clipId
      && review.scope === "all_annotations"
      && review.decision === "approve"
      && review.reviewer?.kind === "human"
      && review.reviewer?.independent === true
      && review.artifacts?.video_sha256 === artifacts.video_sha256
      && review.artifacts?.tracks_sha256 === artifacts.tracks_sha256
      && review.artifacts?.source_sha256 === artifacts.source_sha256)
    .map((review) => review.reviewer.id)).size;
}

async function roots() {
  const found = [];
  for (const [directory, collection] of [["datasets", "dataset"], ["examples", "example"]]) {
    for (const entry of await readdir(path.join(repository, directory), { withFileTypes: true })) {
      if (entry.isDirectory()) found.push({ collection, path: `${directory}/${entry.name}` });
    }
  }
  return found;
}

function title(id) {
  const parts = id.split("-");
  return (/[0-9]/.test(parts[1] ?? "") ? parts.slice(2) : parts).join(" ").replace(/^./, (letter) => letter.toUpperCase());
}

async function readDataset(declaration) {
  const root = path.join(repository, declaration.path);
  const descriptor = parseYaml(await readFile(path.join(root, "dataset.yaml"), "utf8"));
  const clips = await Promise.all(descriptor.clips.map(async ({ id, path: clipPath }) => {
    if (clipPath !== `clips/${id}`) throw new Error(`${declaration.path} contains an invalid clip path`);
    const clipRoot = path.join(root, clipPath);
    const sourceBody = await readFile(path.join(clipRoot, "source.json"));
    const tracksBody = await readFile(path.join(clipRoot, "tracks.jsonl"));
    const source = JSON.parse(sourceBody);
    const reviews = await jsonLines(path.join(clipRoot, "review.jsonl"));
    const model = source.model_runs?.[0]?.model_name ?? null;
    const sourceSha256 = required(source.source?.sha256, `${id}.source.sha256`);
    const artifacts = {
      video_sha256: sourceSha256,
      tracks_sha256: sha256(tracksBody),
      source_sha256: sha256(sourceBody),
    };
    return {
      id,
      title: title(id),
      path: clipPath,
      sourceTitle: required(source.source?.title, `${id}.source.title`),
      sourceUri: required(source.source?.uri, `${id}.source.uri`),
      sourceSha256,
      license: {
        name: required(source.source?.license?.name, `${id}.license.name`),
        spdx: required(source.source?.license?.spdx, `${id}.license.spdx`),
        url: required(source.source?.license?.url, `${id}.license.url`),
      },
      media: {
        frameCount: required(source.media?.frame_count, `${id}.media.frame_count`),
        width: required(source.media?.width, `${id}.media.width`),
        height: required(source.media?.height, `${id}.media.height`),
        fpsNumerator: required(source.media?.fps_numerator, `${id}.media.fps_numerator`),
        fpsDenominator: required(source.media?.fps_denominator, `${id}.media.fps_denominator`),
      },
      annotationRows: tracksBody.toString("utf8").split("\n").filter(Boolean).length,
      humanApprovals: humanApprovals(reviews, id, artifacts),
      model,
      preview: await preview(id),
    };
  }));
  return {
    id: required(descriptor.id, `${declaration.path}.id`),
    title: required(descriptor.title, `${declaration.path}.title`),
    description: required(descriptor.description, `${declaration.path}.description`),
    path: declaration.path,
    collection: declaration.collection,
    version: required(descriptor.version, `${declaration.path}.version`),
    state: required(descriptor.state, `${declaration.path}.state`),
    dataRole: required(descriptor.data_role, `${declaration.path}.data_role`),
    annotationScope: required(descriptor.annotation_scope, `${declaration.path}.annotation_scope`),
    evaluationEligible: descriptor.evaluation_eligible === true,
    requiredIndependentApprovals: required(descriptor.certification?.required_independent_approvals, `${declaration.path}.certification`),
    clips: clips.sort((left, right) => left.id.localeCompare(right.id)),
  };
}

const sourceRevision = revision();
const datasets = await Promise.all((await roots()).map(readDataset));
datasets.sort((left, right) => left.collection.localeCompare(right.collection) || left.id.localeCompare(right.id));
if (revision() !== sourceRevision) throw new Error("dataset repository changed while syncing");
const catalog = {
  schemaVersion: "cvbench.dataset-catalog/v1",
  repository: { name: "cvbench-dataset", url: REPOSITORY_URL, revision: sourceRevision },
  datasets,
};
await writeFile(OUTPUT, `${JSON.stringify(catalog, null, 2)}\n`);
process.stdout.write(`Synced ${datasets.length} datasets and ${datasets.flatMap((dataset) => dataset.clips).length} clips from ${catalog.repository.revision}.\n`);
