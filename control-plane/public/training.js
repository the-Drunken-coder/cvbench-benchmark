const CATALOG_URL = "/training-media/v1/catalog.json";

function byId(id) {
  return document.getElementById(id);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function sameOriginUrl(value) {
  const url = new URL(value, location.origin);
  if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) {
    throw new Error("Training media must be same-origin.");
  }
  return url.href;
}

function formatBytes(bytes) {
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

function appendFact(list, term, value) {
  list.append(element("dt", term), element("dd", String(value)));
}

function sourceLink(label, url) {
  const link = element("a", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function renderVideo(video) {
  const card = element("article", undefined, "training-video-card");
  const stage = element("div", undefined, "training-video-stage");
  const player = element("video");
  player.controls = true;
  player.preload = "metadata";
  player.playsInline = true;
  player.src = sameOriginUrl(video.media.url);
  player.poster = sameOriginUrl(video.poster.url);
  player.setAttribute("aria-label", `${video.title} annotated training preview`);
  stage.append(player);

  const copy = element("div", undefined, "training-video-copy");
  const meta = element("p", `${video.split} split · ${video.sample_count} frames · ${video.annotation_count} proposals`, "kicker");
  const title = element("h3", video.title);
  const description = element("p", video.description);
  const facts = element("dl", undefined, "training-video-facts");
  appendFact(facts, "Preview", `${video.media.width}×${video.media.height} · ${video.media.fps} FPS · ${video.media.duration_seconds.toFixed(1)} s`);
  appendFact(facts, "Classes", Object.entries(video.class_counts).map(([name, count]) => `${name} ${count}`).join(" · "));
  appendFact(facts, "Asset", `${formatBytes(video.media.bytes)} · sha256:${video.media.sha256.slice(0, 16)}…`);
  const provenance = element("p", undefined, "training-provenance");
  provenance.append("Source: ", sourceLink(`${video.creator} on ${video.license_name.includes("Pixabay") ? "Pixabay" : "Pexels"}`, video.source_url), " · ", sourceLink(video.license_name, video.license_url));
  copy.append(meta, title, description, facts, provenance);
  card.append(stage, copy);
  return card;
}

function validateCatalog(catalog) {
  if (
    catalog?.schema_version !== "cvbench.training-media-catalog/v1"
    || catalog.data_role !== "model_training_only"
    || catalog.evaluation_eligible !== false
    || catalog.unknown_is_background !== false
    || catalog.video_count !== 5
    || !Array.isArray(catalog.videos)
    || catalog.videos.length !== catalog.video_count
  ) {
    throw new Error("The training-media catalog boundary is invalid.");
  }
  return catalog;
}

async function loadCatalog() {
  const status = byId("training-status");
  try {
    const response = await fetch(CATALOG_URL, { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`Catalog request failed (${response.status}).`);
    const catalog = validateCatalog(await response.json());
    byId("training-video-count").textContent = String(catalog.video_count);
    byId("training-sample-count").textContent = String(catalog.summary.sample_count);
    byId("training-annotation-count").textContent = String(catalog.summary.annotation_count);
    const list = byId("training-video-list");
    list.replaceChildren(...catalog.videos.map(renderVideo));
    status.textContent = `${catalog.video_count} transformed training previews loaded. None are evaluation-eligible.`;
  } catch (error) {
    status.classList.add("error");
    status.textContent = `Training videos unavailable: ${error.message}`;
  }
}

loadCatalog();
