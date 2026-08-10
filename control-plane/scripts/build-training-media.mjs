import { createHash } from "node:crypto";
import { cp, lstat, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const MAX_ASSET_BYTES = 25 * 1024 * 1024;
const EXPECTED_VIDEO_IDS = new Set([
  "pexels-18187166-dune",
  "pixabay-112059-dog-road",
  "pixabay-145851-forest-bench",
  "pixabay-212474-forest-walk",
  "pixabay-28855-ravine",
]);
const DERIVATION_FIELDS = new Set([
  "clean_source_media_redistributed",
  "preview_only",
  "sample_fps",
  "transformation",
]);
const LABELER_FIELDS = new Set([
  "confidence_threshold",
  "input_size",
  "model",
  "model_sha256",
  "nms_iou_threshold",
  "opencv_version",
  "runtime_image_id",
]);
const SUMMARY_FIELDS = new Set([
  "annotation_count",
  "class_counts",
  "confidence_max",
  "confidence_median",
  "confidence_min",
  "empty_sample_count",
  "positive_training_sample_count",
  "positive_validation_sample_count",
  "sample_count",
  "unique_videos",
]);
const ROOT_FIELDS = new Set([
  "annotation_scope",
  "annotation_status",
  "data_role",
  "derivation",
  "description",
  "evaluation_eligible",
  "id",
  "labeler",
  "ontology",
  "schema_version",
  "summary",
  "title",
  "unknown_is_background",
  "videos",
]);
const VIDEO_FIELDS = new Set([
  "annotation_count",
  "class_counts",
  "creator",
  "description",
  "id",
  "license_name",
  "license_url",
  "preview_bytes",
  "preview_duration_seconds",
  "preview_fps",
  "preview_height",
  "preview_path",
  "preview_sha256",
  "preview_width",
  "poster_bytes",
  "poster_path",
  "poster_sha256",
  "sample_count",
  "source_sha256",
  "source_url",
  "split",
  "title",
]);

function fail(message) {
  throw new Error(`training media build rejected: ${message}`);
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

function exactFields(value, allowed, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} must be an object`);
  const unknown = Object.keys(value).filter((field) => !allowed.has(field));
  if (unknown.length) fail(`${label} contains undeclared fields: ${unknown.join(", ")}`);
}

function requiredString(value, label) {
  if (typeof value !== "string" || !value.trim()) fail(`${label} must be a non-empty string`);
  return value;
}

function requiredSha(value, label) {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) fail(`${label} must be a lowercase sha256`);
  return value;
}

function requiredPositiveNumber(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) fail(`${label} must be positive and finite`);
  return value;
}

function requiredPositiveInteger(value, label) {
  if (!Number.isInteger(value) || value <= 0) fail(`${label} must be a positive integer`);
  return value;
}

function requiredSourceUrl(value, label) {
  requiredString(value, label);
  const url = new URL(value);
  if (url.protocol !== "https:") fail(`${label} must use HTTPS`);
  if (!["pixabay.com", "www.pixabay.com", "pexels.com", "www.pexels.com"].includes(url.hostname)) {
    fail(`${label} must use an allowlisted source host`);
  }
  return value;
}

async function assertRegularAsset(file, sourceRoot) {
  const relative = path.relative(sourceRoot, file);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) fail(`asset escapes source root: ${file}`);
  let current = sourceRoot;
  for (const component of relative.split(path.sep)) {
    current = path.join(current, component);
    const info = await lstat(current);
    if (info.isSymbolicLink()) fail(`asset cannot contain symlinks: ${relative}`);
  }
  const info = await lstat(file);
  if (!info.isFile()) fail(`asset is not a regular file: ${relative}`);
  return info;
}

async function publishJson(output, relative, value) {
  const body = Buffer.from(canonicalJson(value));
  const destination = path.join(output, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, body);
  return { url: `/${relative}`, sha256: sha256(body), bytes: body.length };
}

function validateManifest(manifest) {
  exactFields(manifest, ROOT_FIELDS, "publication");
  exactFields(manifest.derivation, DERIVATION_FIELDS, "publication.derivation");
  exactFields(manifest.labeler, LABELER_FIELDS, "publication.labeler");
  exactFields(manifest.summary, SUMMARY_FIELDS, "publication.summary");
  exactFields(manifest.summary.class_counts, new Set(["dog", "person"]), "publication.summary.class_counts");
  if (manifest.schema_version !== "cvbench.training-media-source/v1") fail("invalid schema_version");
  if (manifest.data_role !== "model_training_only" || manifest.evaluation_eligible !== false) {
    fail("publication must remain training-only and evaluation-ineligible");
  }
  if (manifest.unknown_is_background !== false) fail("unknown samples must not become background negatives");
  if (manifest.derivation?.clean_source_media_redistributed !== false || manifest.derivation?.preview_only !== true) {
    fail("only transformed preview media may be published");
  }
  if (manifest.derivation.sample_fps !== 5 || !manifest.derivation.transformation.includes("burned-in")) {
    fail("publication must retain the reviewed 5 FPS annotated-preview transformation");
  }
  if (manifest.summary?.unique_videos !== 5 || manifest.summary?.sample_count !== 504 || manifest.summary?.annotation_count !== 503) {
    fail("publication summary does not match the reviewed corpus");
  }
  if (!Array.isArray(manifest.videos) || manifest.videos.length !== 5) fail("publication must contain five videos");
  if (JSON.stringify(manifest.ontology) !== JSON.stringify(["person", "dog"])) fail("publication ontology drifted");
  requiredString(manifest.labeler.model, "publication.labeler.model");
  requiredSha(manifest.labeler.model_sha256, "publication.labeler.model_sha256");
  requiredString(manifest.labeler.runtime_image_id, "publication.labeler.runtime_image_id");
  if (manifest.summary.class_counts.person !== 468 || manifest.summary.class_counts.dog !== 35) {
    fail("publication class totals do not match the reviewed corpus");
  }
  return manifest;
}

async function publishTrainingMedia(root, output) {
  const sourceRoot = path.join(root, "training-media/recovered-videos-v1");
  const manifestPath = path.join(sourceRoot, "publication.json");
  await assertRegularAsset(manifestPath, sourceRoot);
  const manifestBody = await readFile(manifestPath);
  const manifest = validateManifest(JSON.parse(manifestBody));
  const seen = new Set();
  const videos = [];
  let totalPreviewBytes = 0;
  let totalSamples = 0;
  let totalAnnotations = 0;
  const totalClasses = { dog: 0, person: 0 };
  for (const source of manifest.videos) {
    exactFields(source, VIDEO_FIELDS, "publication video");
    const id = requiredString(source.id, "video.id");
    if (!EXPECTED_VIDEO_IDS.has(id) || seen.has(id)) fail(`invalid or duplicate video id: ${id}`);
    seen.add(id);
    if (source.preview_path !== `previews/${id}.mp4`) fail(`video ${id} has an unexpected preview path`);
    const asset = path.join(sourceRoot, source.preview_path);
    const info = await assertRegularAsset(asset, sourceRoot);
    requiredPositiveNumber(source.preview_bytes, `${id}.preview_bytes`);
    if (info.size !== source.preview_bytes) fail(`video ${id} byte count drifted`);
    if (info.size > MAX_ASSET_BYTES) fail(`video ${id} exceeds Cloudflare's 25 MiB asset limit`);
    const body = await readFile(asset);
    if (body.subarray(4, 8).toString("ascii") !== "ftyp") fail(`video ${id} is not an MP4 asset`);
    const digest = sha256(body);
    if (digest !== requiredSha(source.preview_sha256, `${id}.preview_sha256`)) fail(`video ${id} hash drifted`);
    requiredSha(source.source_sha256, `${id}.source_sha256`);
    requiredSourceUrl(source.source_url, `${id}.source_url`);
    requiredSourceUrl(source.license_url, `${id}.license_url`);
    for (const field of ["title", "description", "creator", "license_name", "split"]) {
      requiredString(source[field], `${id}.${field}`);
    }
    for (const field of ["sample_count", "annotation_count", "preview_width", "preview_height", "preview_fps", "preview_duration_seconds"]) {
      requiredPositiveNumber(source[field], `${id}.${field}`);
    }
    requiredPositiveInteger(source.sample_count, `${id}.sample_count`);
    requiredPositiveInteger(source.annotation_count, `${id}.annotation_count`);
    if (source.preview_width !== 960 || source.preview_height !== 540 || source.preview_fps !== 5) {
      fail(`video ${id} preview envelope drifted`);
    }
    if (Math.abs(source.preview_duration_seconds - source.sample_count / source.preview_fps) > 1e-9) {
      fail(`video ${id} duration does not match its sampled frames`);
    }
    if (!["training", "validation"].includes(source.split)) fail(`video ${id} has an invalid split`);
    exactFields(source.class_counts, new Set(["dog", "person"]), `${id}.class_counts`);
    const classKeys = Object.keys(source.class_counts).sort();
    if (!classKeys.length || classKeys.some((classId) => !["dog", "person"].includes(classId))) {
      fail(`video ${id} class counts are outside the publication ontology`);
    }
    let videoClassTotal = 0;
    for (const classId of classKeys) {
      requiredPositiveInteger(source.class_counts[classId], `${id}.class_counts.${classId}`);
      totalClasses[classId] += source.class_counts[classId];
      videoClassTotal += source.class_counts[classId];
    }
    if (videoClassTotal !== source.annotation_count) fail(`video ${id} class counts do not match annotation_count`);
    const relative = `training-media/v1/assets/sha256/${digest}.mp4`;
    const destination = path.join(output, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    await cp(asset, destination);
    if (source.poster_path !== `posters/${id}.jpg`) fail(`video ${id} has an unexpected poster path`);
    const posterAsset = path.join(sourceRoot, source.poster_path);
    const posterInfo = await assertRegularAsset(posterAsset, sourceRoot);
    if (posterInfo.size !== source.poster_bytes || posterInfo.size > MAX_ASSET_BYTES) {
      fail(`video ${id} poster byte count drifted or exceeds the asset limit`);
    }
    const posterBody = await readFile(posterAsset);
    if (!posterBody.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff]))) {
      fail(`video ${id} poster is not a JPEG asset`);
    }
    const posterDigest = sha256(posterBody);
    if (posterDigest !== requiredSha(source.poster_sha256, `${id}.poster_sha256`)) {
      fail(`video ${id} poster hash drifted`);
    }
    const posterRelative = `training-media/v1/assets/sha256/${posterDigest}.jpg`;
    const posterDestination = path.join(output, posterRelative);
    await mkdir(path.dirname(posterDestination), { recursive: true });
    await cp(posterAsset, posterDestination);
    totalPreviewBytes += info.size;
    totalSamples += source.sample_count;
    totalAnnotations += source.annotation_count;
    videos.push({
      id,
      title: source.title,
      description: source.description,
      creator: source.creator,
      source_url: source.source_url,
      source_sha256: source.source_sha256,
      license_name: source.license_name,
      license_url: source.license_url,
      split: source.split,
      sample_count: source.sample_count,
      annotation_count: source.annotation_count,
      class_counts: source.class_counts,
      media: {
        url: `/${relative}`,
        sha256: digest,
        bytes: info.size,
        content_type: "video/mp4",
        width: source.preview_width,
        height: source.preview_height,
        fps: source.preview_fps,
        duration_seconds: source.preview_duration_seconds,
      },
      poster: {
        url: `/${posterRelative}`,
        sha256: posterDigest,
        bytes: posterInfo.size,
        content_type: "image/jpeg",
      },
    });
  }
  if (seen.size !== EXPECTED_VIDEO_IDS.size || [...EXPECTED_VIDEO_IDS].some((id) => !seen.has(id))) {
    fail("publication does not contain the exact reviewed video set");
  }
  if (
    totalSamples !== manifest.summary.sample_count
    || totalAnnotations !== manifest.summary.annotation_count
    || totalClasses.person !== manifest.summary.class_counts.person
    || totalClasses.dog !== manifest.summary.class_counts.dog
  ) {
    fail("publication video aggregates do not match the corpus summary");
  }
  const catalog = {
    schema_version: "cvbench.training-media-catalog/v1",
    id: manifest.id,
    title: manifest.title,
    description: manifest.description,
    status: "public",
    data_role: manifest.data_role,
    evaluation_eligible: false,
    annotation_scope: manifest.annotation_scope,
    annotation_status: manifest.annotation_status,
    unknown_is_background: false,
    ontology: manifest.ontology,
    labeler: manifest.labeler,
    summary: manifest.summary,
    derivation: manifest.derivation,
    video_count: videos.length,
    videos,
  };
  const catalogOutput = await publishJson(output, "training-media/v1/catalog.json", catalog);
  await publishJson(output, ".well-known/cvbench-training-media.json", {
    schema_version: "cvbench.training-media-discovery/v1",
    catalog_url: catalogOutput.url,
    catalog_sha256: catalogOutput.sha256,
    video_count: videos.length,
    data_role: "model_training_only",
    evaluation_eligible: false,
  });
  await publishJson(output, "training-media/v1/build-evidence.json", {
    schema_version: "cvbench.training-media-build/v1",
    source_manifest_sha256: sha256(manifestBody),
    video_count: videos.length,
    total_preview_bytes: totalPreviewBytes,
    assets: videos.map((video) => video.media),
  });
  return { videos: videos.length, bytes: totalPreviewBytes };
}

export { publishTrainingMedia };
