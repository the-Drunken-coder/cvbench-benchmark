import { createLatestScenarioLoader, exactFrameFailureMessage, renderExactFrameFailure } from "/scenario-loader.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_DECODED_BYTES = 48 * 1024 * 1024;
const MAX_DECODE_CONCURRENCY = 4;
const PLAYBACK_AHEAD_NS = 750_000_000;
const PLAYBACK_START_BUFFER_NS = 250_000_000;
const SAVE_DATA_AHEAD_NS = 150_000_000;
const BUFFERING_ANNOUNCEMENT_DELAY_MS = 500;
const detailLoader = createLatestScenarioLoader();
const state = {
  catalog: null,
  detail: null,
  frames: null,
  annotations: null,
  baseline: null,
  selected: 0,
  playing: false,
  playbackSpeed: 1,
  animationFrame: null,
  playbackAnchorMs: null,
  playbackAnchorSourceNs: null,
  playbackGeneration: 0,
  buffering: false,
  bufferingAnnouncementTimer: null,
  bufferingAnnounced: false,
  frameCache: new Map(),
  decodedBytes: 0,
  decodeQueue: [],
  activeDecodes: 0,
  generation: 0,
};

const byId = (id) => document.getElementById(id);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function appendDefinition(list, term, value) {
  list.append(element("dt", term), element("dd", value ?? "Not declared"));
}

function humanBytes(bytes) {
  if (!Number.isFinite(bytes)) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function formatTime(ns) {
  return `${(ns / 1_000_000_000).toFixed(3)} s`;
}

function setCatalogStatus(message, error = false) {
  const output = byId("catalog-status");
  output.textContent = message;
  output.classList.toggle("error", error);
}

async function fetchJson(url, signal) {
  const response = await fetch(url, { headers: { accept: "application/json" }, signal });
  if (!response.ok) throw new Error(`Could not load ${url} (${response.status}).`);
  return response.json();
}

function scenarioMatches(scenario) {
  const query = byId("scenario-search").value.trim().toLocaleLowerCase();
  const pack = byId("pack-filter").value;
  if (pack !== "all" && scenario.pack.id !== pack) return false;
  if (!query) return true;
  return [scenario.id, scenario.title, scenario.description, ...scenario.failure_modes]
    .join(" ")
    .toLocaleLowerCase()
    .includes(query);
}

function renderCatalog() {
  const list = byId("scenario-list");
  list.replaceChildren();
  const scenarios = state.catalog.scenarios.filter(scenarioMatches);
  for (const scenario of scenarios) {
    const card = element("article", undefined, "scenario-card");
    const meta = element("p", `${scenario.pack.id} · ${scenario.frames} frames · ${scenario.resolution}`, "scenario-card-meta");
    const title = element("h3");
    const link = element("a", scenario.title);
    link.href = `/scenarios/?scenario=${encodeURIComponent(scenario.id)}`;
    title.append(link);
    const description = element("p", scenario.description);
    const tags = element("ul", undefined, "scenario-tags");
    for (const mode of scenario.failure_modes) tags.append(element("li", mode));
    const footer = element("p", undefined, "scenario-card-footer");
    footer.append(element("span", scenario.license), element("span", scenario.annotation_scope.replaceAll("_", " ")));
    card.append(meta, title, description, tags, footer);
    list.append(card);
  }
  setCatalogStatus(`${scenarios.length} of ${state.catalog.scenario_count} public scenarios shown.`);
}

function currentAnnotationFrame() {
  return state.annotations.frames[state.selected];
}

function realVideoDetail() {
  return state.detail?.pack?.id === "real-video-v2";
}

function motChallengeDetail() {
  return state.detail?.pack?.id === "motchallenge-v1";
}

function faultLabels(frameIndex) {
  const labels = [];
  for (const fault of state.annotations.faults || []) {
    if (Array.isArray(fault.frame_indices) && fault.frame_indices.includes(frameIndex)) labels.push(fault.type);
    if (fault.after_frame === frameIndex) labels.push(`${fault.type} after frame`);
  }
  return labels;
}

function svgNode(tag, attributes) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function drawBox(svg, box, kind, label, showLabel, frameWidth) {
  const [x1, y1, x2, y2] = box;
  svg.append(svgNode("rect", { x: x1, y: y1, width: x2 - x1, height: y2 - y1, class: `overlay-box ${kind}` }));
  if (showLabel && label) {
    const fontSize = Math.max(3, frameWidth * 0.014);
    const text = svgNode("text", { x: x1 + fontSize * 0.3, y: Math.max(y1 + fontSize, fontSize), "font-size": fontSize, "stroke-width": frameWidth * 0.003, class: `overlay-label ${kind}` });
    text.textContent = label;
    svg.append(text);
  }
}

function overlayEnabled(name) {
  return document.querySelector(`[data-overlay="${name}"]`).checked;
}

function renderOverlay() {
  const svg = byId("scenario-overlay");
  const frame = state.frames.frames[state.selected];
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${frame.width} ${frame.height}`);
  const region = state.annotations.scoreable_region;
  if (overlayEnabled("roi") && region?.type !== "full_frame") {
    drawBox(svg, region.bounds, "roi", "scoreable boundary", overlayEnabled("labels"), frame.width);
  }
  for (const object of currentAnnotationFrame().objects) {
    if (!object.bbox_xyxy) continue;
    const ignored = Boolean(object.ignore);
    if ((ignored && !overlayEnabled("ignores")) || (!ignored && !overlayEnabled("targets"))) continue;
    const kind = ignored ? "ignore" : "target";
    const label = ignored ? `${object.ignore_region ? "ignore region" : "ignore"}: ${object.ignore_region_id || object.target_id}` : `${object.class_id}: ${object.target_id}`;
    drawBox(svg, object.bbox_xyxy, kind, label, overlayEnabled("labels"), frame.width);
  }
  const faults = faultLabels(frame.frame_index);
  if (overlayEnabled("faults") && faults.length) {
    const group = svgNode("g", { class: "fault-overlay" });
    group.append(svgNode("rect", { x: 0, y: 0, width: frame.width, height: frame.height * 0.08 }));
    const text = svgNode("text", { x: frame.width * 0.01, y: Math.max(frame.width * 0.025, frame.height * 0.055), "font-size": frame.width * 0.02 });
    text.textContent = `FAULT · ${faults.join(", ")}`;
    group.append(text);
    svg.append(group);
  }
}

function renderInspector() {
  const frame = state.frames.frames[state.selected];
  const annotations = currentAnnotationFrame().objects;
  const facts = byId("frame-facts");
  facts.replaceChildren();
  appendDefinition(facts, "Index", frame.frame_index);
  appendDefinition(facts, "Source time", formatTime(frame.source_timestamp_ns));
  appendDefinition(facts, "Resolution", `${frame.width} × ${frame.height}`);
  appendDefinition(facts, "JPEG", `${humanBytes(frame.media.bytes)} · sha256:${frame.media.sha256.slice(0, 16)}…`);
  appendDefinition(facts, realVideoDetail() ? "Tracked movers" : "Targets", annotations.filter((row) => !row.ignore).length);
  if (!realVideoDetail()) appendDefinition(facts, "Ignores", annotations.filter((row) => row.ignore).length);
  appendDefinition(facts, "Faults", faultLabels(frame.frame_index).join(", ") || "None");
  const list = byId("frame-annotations");
  list.replaceChildren();
  if (!annotations.length) list.append(element("p", realVideoDetail()
    ? "No supported moving objects are visible on this frame. This is explicit exhaustive ground truth, not a loading error."
    : "No target or ignore rows on this frame. This is explicit public ground truth, not a loading error."));
  for (const object of annotations) {
    const row = element("article");
    row.append(element("strong", object.ignore ? (object.ignore_region ? "Ignore region" : "Ignore") : (realVideoDetail() ? "Tracked object" : "Target")));
    row.append(element("span", `${object.target_id} · ${object.class_id}`));
    if (object.bbox_xyxy) row.append(element("code", `[${object.bbox_xyxy.join(", ")}]`));
    if (!object.ignore) {
      const visibility = object.visibility_fraction === null
        ? "visibility/occlusion not independently labeled"
        : `${object.occlusion} occlusion · ${(object.visibility_fraction * 100).toFixed(0)}% visible`;
      row.append(element("small", `${visibility} · ${object.truncated ? "truncated" : "not truncated"} · ${object.eligible_for_detection ? "scoreable" : "not detection-eligible"}`));
    }
    list.append(row);
  }
}

async function digestHex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function sameOriginUrl(url) {
  const resolved = new URL(url, location.origin);
  if (resolved.origin !== location.origin || !["http:", "https:"].includes(resolved.protocol)) {
    throw new Error("Exact frame URL must be same-origin.");
  }
  return resolved.href;
}

function hideMediaStatus() {
  const mediaState = byId("media-state");
  if (!mediaState.hidden) mediaState.hidden = true;
  if (mediaState.classList.contains("error")) mediaState.classList.remove("error");
  if (mediaState.textContent) mediaState.textContent = "";
}

function reportDecodeState() {
  if (typeof globalThis.__cvbenchReportDecodeState !== "function") return;
  globalThis.__cvbenchReportDecodeState({
    decodedBytes: state.decodedBytes,
    liveEntries: [...state.frameCache.values()].filter((entry) => entry.status === "ready").length,
  });
}

function removeOwnedCacheEntry(entry) {
  if (state.frameCache.get(entry.index) !== entry) return false;
  state.frameCache.delete(entry.index);
  return true;
}

function closeCacheEntry(entry) {
  if (!removeOwnedCacheEntry(entry)) return;
  if (entry.status === "ready") {
    entry.bitmap.close();
    state.decodedBytes -= entry.decodedBytes;
  }
  reportDecodeState();
}

function resetFrameCache() {
  for (const entry of state.frameCache.values()) {
    if (entry.status === "ready") entry.bitmap.close();
    else entry.controller.abort();
    if (entry.status === "queued") {
      entry.status = "cancelled";
      entry.reject(new DOMException("Stale frame request.", "AbortError"));
    }
  }
  state.frameCache.clear();
  state.decodeQueue.length = 0;
  state.decodedBytes = 0;
  state.generation += 1;
  reportDecodeState();
  const canvas = byId("scenario-frame");
  canvas.getContext("2d", { alpha: false }).clearRect(0, 0, canvas.width, canvas.height);
  canvas.dataset.frameReady = "false";
  canvas.removeAttribute("data-frame-index");
  byId("scenario-overlay").replaceChildren();
}

function cancelPendingFramesExcept(keepIndex = null) {
  for (const entry of [...state.frameCache.values()]) {
    if (entry.index === keepIndex || !["queued", "loading"].includes(entry.status)) continue;
    entry.controller.abort();
    state.frameCache.delete(entry.index);
    if (entry.status === "queued") {
      entry.status = "cancelled";
      entry.reject(new DOMException("Superseded frame request.", "AbortError"));
    }
  }
  state.decodeQueue = state.decodeQueue.filter((entry) => entry.status === "queued");
}

function trimFrameCache(focusIndex) {
  const entries = [...state.frameCache.values()];
  for (const entry of entries) {
    if (entry.status === "ready" && entry.index < focusIndex - 2) closeCacheEntry(entry);
  }
  if (state.decodedBytes <= MAX_DECODED_BYTES) return;
  const removable = [...state.frameCache.values()]
    .filter((entry) => entry.status === "ready" && entry.index !== state.selected)
    .sort((left, right) => Math.abs(right.index - focusIndex) - Math.abs(left.index - focusIndex));
  for (const entry of removable) {
    closeCacheEntry(entry);
    if (state.decodedBytes <= MAX_DECODED_BYTES) break;
  }
}

async function decodeFrame(entry) {
  const frame = state.frames.frames[entry.index];
  const response = await fetch(sameOriginUrl(frame.media.url), {
    cache: "force-cache",
    signal: entry.controller.signal,
  });
  if (!response.ok) throw new Error(exactFrameFailureMessage(response.status));
  const body = await response.arrayBuffer();
  if (await digestHex(body) !== frame.media.sha256) {
    throw new Error("Exact frame failed its published SHA-256 check.");
  }
  const bitmap = await createImageBitmap(new Blob([body], { type: "image/jpeg" }));
  if (bitmap.width !== frame.width || bitmap.height !== frame.height) {
    bitmap.close();
    throw new Error("The verified media decoded at an unexpected resolution.");
  }
  if (entry.generation !== state.generation || state.frameCache.get(entry.index) !== entry) {
    bitmap.close();
    throw new DOMException("Stale frame decode.", "AbortError");
  }
  entry.bitmap = bitmap;
  entry.decodedBytes = frame.width * frame.height * 4;
  entry.status = "ready";
  state.decodedBytes += entry.decodedBytes;
  trimFrameCache(Math.min(state.selected, entry.index));
  reportDecodeState();
  return entry;
}

function pumpDecodeQueue() {
  while (state.activeDecodes < MAX_DECODE_CONCURRENCY && state.decodeQueue.length) {
    const entry = state.decodeQueue.shift();
    if (entry.generation !== state.generation || entry.status !== "queued") continue;
    entry.status = "loading";
    state.activeDecodes += 1;
    decodeFrame(entry).then(entry.resolve, (error) => {
      entry.status = "error";
      entry.error = error;
      removeOwnedCacheEntry(entry);
      entry.reject(error);
    }).finally(() => {
      state.activeDecodes -= 1;
      pumpDecodeQueue();
    });
  }
}

function requestDecodedFrame(index) {
  const cached = state.frameCache.get(index);
  if (cached && cached.status !== "error") return cached.promise;
  if (cached) removeOwnedCacheEntry(cached);
  const entry = {
    controller: new AbortController(),
    decodedBytes: 0,
    generation: state.generation,
    index,
    status: "queued",
  };
  entry.promise = new Promise((resolve, reject) => {
    entry.resolve = resolve;
    entry.reject = reject;
  });
  state.frameCache.set(index, entry);
  state.decodeQueue.push(entry);
  pumpDecodeQueue();
  return entry.promise;
}

function frameIndexAtOrBefore(sourceTimestampNs) {
  const frames = state.frames.frames;
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (frames[middle].source_timestamp_ns <= sourceTimestampNs) low = middle;
    else high = middle - 1;
  }
  return low;
}

function frameIndexThroughDuration(startIndex, durationNs) {
  const start = state.frames.frames[startIndex].source_timestamp_ns;
  return frameIndexAtOrBefore(start + durationNs);
}

function updateFrameUi(index, announcement = false) {
  state.selected = index;
  const frame = state.frames.frames[index];
  byId("frame-scrubber").value = String(index);
  byId("frame-position").textContent = `${index + 1} / ${state.frames.frames.length}`;
  byId("frame-time").textContent = formatTime(frame.source_timestamp_ns);
  byId("previous-frame").disabled = index === 0;
  byId("next-frame").disabled = index === state.frames.frames.length - 1;
  renderOverlay();
  renderInspector();
  if (announcement) {
    byId("viewer-announcement").textContent = `Frame ${index + 1} of ${state.frames.frames.length}, ${annotationsSummary()}.`;
  }
}

function presentFrame(index, entry, announcement = false) {
  const frame = state.frames.frames[index];
  const canvas = byId("scenario-frame");
  if (canvas.width !== frame.width || canvas.height !== frame.height) {
    canvas.width = frame.width;
    canvas.height = frame.height;
  }
  const context = canvas.getContext("2d", { alpha: false });
  context.drawImage(entry.bitmap, 0, 0);
  canvas.dataset.frameReady = "true";
  canvas.dataset.frameIndex = String(index);
  canvas.setAttribute("aria-label", `${state.detail.title}, exact frame ${frame.frame_index + 1} of ${state.frames.frames.length}`);
  updateFrameUi(index, announcement);
  hideMediaStatus();
  trimFrameCache(index);
}

async function prefetchRange(startIndex, endIndex) {
  const work = [];
  for (let index = startIndex; index <= endIndex; index += 1) {
    work.push(requestDecodedFrame(index));
  }
  return Promise.all(work);
}

function prefetchPosterNeighbors(index) {
  if (navigator.connection?.saveData) return;
  const last = state.frames.frames.length - 1;
  void prefetchRange(index + 1, Math.min(index + 2, last)).catch(() => {});
}

async function showFrame(index, announcement = true) {
  if (!state.frames) return false;
  stopPlayback();
  const selected = Math.max(0, Math.min(index, state.frames.frames.length - 1));
  cancelPendingFramesExcept(selected);
  const generation = ++state.playbackGeneration;
  hideMediaStatus();
  try {
    const entry = await requestDecodedFrame(selected);
    if (generation !== state.playbackGeneration || entry.generation !== state.generation) return false;
    presentFrame(selected, entry, announcement);
    prefetchPosterNeighbors(selected);
    return true;
  } catch (error) {
    if (generation !== state.playbackGeneration || error?.name === "AbortError") return false;
    renderExactFrameFailure(byId("media-state"), error);
    byId("viewer-announcement").textContent = error.message;
    return false;
  }
}

function annotationsSummary() {
  const rows = currentAnnotationFrame().objects;
  if (realVideoDetail()) return `${rows.filter((row) => !row.ignore).length} tracked moving objects`;
  return `${rows.filter((row) => !row.ignore).length} targets and ${rows.filter((row) => row.ignore).length} ignores`;
}

function stopPlayback() {
  if (state.animationFrame) window.cancelAnimationFrame(state.animationFrame);
  if (state.bufferingAnnouncementTimer) window.clearTimeout(state.bufferingAnnouncementTimer);
  state.animationFrame = null;
  state.playbackAnchorMs = null;
  state.playbackAnchorSourceNs = null;
  state.buffering = false;
  state.bufferingAnnouncementTimer = null;
  state.bufferingAnnounced = false;
  state.playing = false;
  const button = byId("play-pause");
  if (button) {
    button.textContent = "Play";
    button.setAttribute("aria-label", "Play sequence");
  }
  if (byId("scenario-frame")?.dataset.frameReady === "true") hideMediaStatus();
}

function setBuffering(buffering, playbackGeneration) {
  if (state.buffering === buffering) return;
  state.buffering = buffering;
  if (state.bufferingAnnouncementTimer) {
    window.clearTimeout(state.bufferingAnnouncementTimer);
    state.bufferingAnnouncementTimer = null;
  }
  if (!buffering) {
    if (state.bufferingAnnounced) {
      state.bufferingAnnounced = false;
      byId("viewer-announcement").textContent = "Playback resumed.";
    }
    return;
  }
  state.bufferingAnnouncementTimer = window.setTimeout(() => {
    state.bufferingAnnouncementTimer = null;
    if (!state.playing
      || !state.buffering
      || playbackGeneration !== state.playbackGeneration) return;
    state.bufferingAnnounced = true;
    byId("viewer-announcement").textContent = "Playback buffering. The current frame remains visible.";
  }, BUFFERING_ANNOUNCEMENT_DELAY_MS);
}

function handlePlaybackDecodeError(error, scenarioGeneration, playbackGeneration) {
  if (!state.playing
    || scenarioGeneration !== state.generation
    || playbackGeneration !== state.playbackGeneration
    || error?.name === "AbortError") return;
  stopPlayback();
  renderExactFrameFailure(byId("media-state"), error);
  byId("viewer-announcement").textContent = `Playback stopped. ${error.message}`;
}

function prefetchPlayback(expectedIndex, playbackGeneration) {
  const duration = navigator.connection?.saveData ? SAVE_DATA_AHEAD_NS : PLAYBACK_AHEAD_NS;
  const frame = state.frames.frames[expectedIndex];
  const decodedFrameBytes = frame.width * frame.height * 4;
  const memoryFrameLimit = Math.max(1, Math.floor(MAX_DECODED_BYTES / decodedFrameBytes) - 1);
  const end = Math.min(
    frameIndexThroughDuration(expectedIndex, duration),
    expectedIndex + memoryFrameLimit,
    state.frames.frames.length - 1,
  );
  const scenarioGeneration = state.generation;
  for (let index = expectedIndex; index <= end; index += 1) {
    void requestDecodedFrame(index)
      .catch((error) => handlePlaybackDecodeError(error, scenarioGeneration, playbackGeneration));
  }
}

function playbackTick(now, playbackGeneration) {
  if (!state.playing || playbackGeneration !== state.playbackGeneration) return;
  const elapsedMs = now - state.playbackAnchorMs;
  const sourceTimestampNs = state.playbackAnchorSourceNs + elapsedMs * 1_000_000 * state.playbackSpeed;
  const expectedIndex = frameIndexAtOrBefore(sourceTimestampNs);
  prefetchPlayback(expectedIndex, playbackGeneration);

  let presentable = null;
  for (let index = expectedIndex; index > state.selected; index -= 1) {
    const entry = state.frameCache.get(index);
    if (entry?.status === "ready") {
      presentable = entry;
      break;
    }
  }
  if (presentable) presentFrame(presentable.index, presentable);
  setBuffering(expectedIndex > state.selected, playbackGeneration);

  const lastIndex = state.frames.frames.length - 1;
  if (expectedIndex === lastIndex && state.selected === lastIndex) {
    stopPlayback();
    byId("viewer-announcement").textContent = `Playback ended on frame ${lastIndex + 1}.`;
    return;
  }
  state.animationFrame = window.requestAnimationFrame(
    (nextNow) => playbackTick(nextNow, playbackGeneration),
  );
}

async function startPlayback() {
  if (state.selected === state.frames.frames.length - 1) {
    const presented = await showFrame(0, false);
    if (!presented) return;
  }
  const requestGeneration = ++state.playbackGeneration;
  const scenarioGeneration = state.generation;
  state.playing = true;
  byId("play-pause").textContent = "Pause";
  byId("play-pause").setAttribute("aria-label", "Pause sequence");
  setBuffering(true, requestGeneration);
  const lastIndex = state.frames.frames.length - 1;
  const bufferEnd = Math.min(frameIndexThroughDuration(state.selected, PLAYBACK_START_BUFFER_NS), lastIndex);
  try {
    await prefetchRange(state.selected, bufferEnd);
  } catch (error) {
    handlePlaybackDecodeError(error, scenarioGeneration, requestGeneration);
    return;
  }
  if (!state.playing || requestGeneration !== state.playbackGeneration) return;
  state.playbackAnchorMs = performance.now();
  state.playbackAnchorSourceNs = state.frames.frames[state.selected].source_timestamp_ns;
  setBuffering(false, requestGeneration);
  byId("viewer-announcement").textContent = `Playing from frame ${state.selected + 1} at ${state.playbackSpeed} times speed.`;
  prefetchPlayback(state.selected, requestGeneration);
  state.animationFrame = window.requestAnimationFrame(
    (now) => playbackTick(now, requestGeneration),
  );
}

function togglePlayback() {
  if (state.playing) {
    state.playbackGeneration += 1;
    stopPlayback();
    byId("viewer-announcement").textContent = `Paused on frame ${state.selected + 1}.`;
    return;
  }
  void startPlayback();
}

function safeLink(label, href) {
  const link = element("a", label);
  const url = new URL(href, location.origin);
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("Unsafe catalog link protocol.");
  link.href = url.href;
  if (url.origin !== location.origin) {
    link.rel = "noreferrer";
    link.target = "_blank";
  }
  return link;
}

function renderDetailFacts() {
  const detail = state.detail;
  const scenario = byId("scenario-facts");
  scenario.replaceChildren();
  appendDefinition(scenario, "Stable ID", detail.id);
  appendDefinition(scenario, "Status", `${detail.status} · ${detail.pack.id} ${detail.pack.version}`);
  appendDefinition(scenario, "Task", detail.task);
  appendDefinition(scenario, "Failure modes", detail.failure_modes.join(", "));
  appendDefinition(scenario, "Timing", `${detail.media.frame_count} frames · ${detail.media.fps ?? "variable"} FPS · ${formatTime(detail.media.duration_ns)}`);
  appendDefinition(scenario, "Resolution", `${detail.media.width} × ${detail.media.height}`);
  appendDefinition(scenario, "Classes", detail.annotations.class_ids.join(", ") || "No targets by design");
  appendDefinition(scenario, "Annotation policy", detail.annotations.policy.disclosure);
  if (detail.annotations.scoring.scoreable_region === "full_frame") {
    appendDefinition(scenario, "Scoring boundary", detail.pack.id === "motchallenge-v1"
      ? "Class-aware, full-frame pedestrian scoring. Marked class-1 pedestrians are exhaustive targets; official non-target rows are neutral only after target matching."
      : "Class-aware, full-frame scoring. Every supported visible mover is exhaustive ground truth; misses, duplicates, false tracks, and background predictions are penalized.");
    appendDefinition(scenario, "Temporal scoring", detail.annotations.scoring.temporal_metrics.join(", "));
  } else {
    appendDefinition(scenario, "Scoring boundary", `Class-aware. ${detail.annotations.scoring.outside_fixed_roi.replaceAll("_", " ")}. Target matching precedes ignore matching.`);
    appendDefinition(scenario, "Ignore rules", `Ignore region: ${detail.annotations.scoring.ignore_region_match}; ordinary ignore: ${detail.annotations.scoring.ordinary_ignore_match}.`);
  }

  const provenance = byId("provenance-facts");
  provenance.replaceChildren();
  provenance.append(element("p", detail.provenance.source.attribution));
  const sourceLine = element("p");
  sourceLine.append(safeLink(detail.provenance.source.title, detail.provenance.source.source_url));
  provenance.append(sourceLine);
  if (detail.provenance.source.license_url) provenance.append(safeLink(detail.provenance.source.license, detail.provenance.source.license_url));
  else provenance.append(element("p", detail.provenance.source.license));
  provenance.append(element("p", detail.provenance.source.transformation));
  if (detail.provenance.source.annotation_provenance) provenance.append(element("p", `Annotation provenance: ${detail.provenance.source.annotation_provenance}`));
  if (detail.provenance.source.corrections) provenance.append(element("p", `Audited corrections: ${detail.provenance.source.corrections}`));
  provenance.append(element("p", `Preparation: ${detail.provenance.preparation.identity} · ${detail.provenance.preparation.platform} · definition sha256:${detail.provenance.preparation.dockerfile_sha256.slice(0, 16)}…`));

  const baseline = byId("baseline-facts");
  baseline.replaceChildren();
  appendDefinition(baseline, "System", `${state.baseline.system_name} · ${state.baseline.system_version}`);
  appendDefinition(baseline, "Status", state.baseline.validation_status);
  appendDefinition(baseline, "Run", state.baseline.run_id);
  appendDefinition(baseline, "Report hash", `sha256:${state.baseline.report_sha256}`);
  appendDefinition(baseline, "Sanitized evidence", `${state.baseline.source_evidence.id} · sha256:${state.baseline.source_evidence.sha256}`);
  for (const [name, value] of Object.entries(state.baseline.metrics)) appendDefinition(baseline, name.replaceAll("_", " "), value);

  const links = byId("manifest-links");
  links.replaceChildren();
  const manifests = [
    ["Stable scenario JSON", `/scenario-catalog/v1/scenarios/${detail.id}.json`, null],
    ["Content-addressed frame manifest", detail.media.frame_manifest.url, detail.media.frame_manifest.sha256],
    ["Content-addressed annotation manifest", detail.annotations.annotation_manifest.url, detail.annotations.annotation_manifest.sha256],
    ["Content-addressed baseline manifest", detail.baseline.manifest.url, detail.baseline.manifest.sha256],
  ];
  if (detail.media.viewer_derivative) {
    manifests.push(["Deterministic publisher-cadence viewer derivative", detail.media.viewer_derivative.url, detail.media.viewer_derivative.sha256]);
    manifests.push(["Normalized full annotation bundle", detail.annotations.normalized_ground_truth.url, detail.annotations.normalized_ground_truth.sha256]);
    manifests.push(["Visual audit overview", detail.media.visual_audit_overview.url, detail.media.visual_audit_overview.sha256]);
  }
  for (const [label, url, hash] of manifests) {
    const item = element("li");
    item.append(safeLink(label, url));
    if (hash) item.append(element("code", `sha256:${hash}`));
    links.append(item);
  }
}

async function loadDetail(id) {
  stopPlayback();
  state.playbackGeneration += 1;
  cancelPendingFramesExcept();
  setCatalogStatus(`Loading ${id}…`);
  const summary = state.catalog.scenarios.find((scenario) => scenario.id === id);
  if (!summary) {
    detailLoader.cancel();
    throw new Error(`Unknown public scenario: ${id}.`);
  }
  await detailLoader.load(id, async (signal) => {
    const detail = await fetchJson(summary.detail.url, signal);
    const [frames, annotations, baseline] = await Promise.all([
      fetchJson(detail.media.frame_manifest.url, signal),
      fetchJson(detail.annotations.annotation_manifest.url, signal),
      fetchJson(detail.baseline.manifest.url, signal),
    ]);
    if (detail.id !== id || frames.scenario_id !== id || annotations.scenario_id !== id || baseline.scenario_id !== id) throw new Error("Scenario manifest identity mismatch.");
    const mot = detail.pack.id === "motchallenge-v1";
    if (!mot && frames.frames.length !== annotations.frames.length) throw new Error("Frame and annotation manifest lengths differ.");
    if (mot && frames.frames.length !== detail.media.frame_count) throw new Error("Exact-frame hash manifest length differs.");
    return { annotations, baseline, detail, frames };
  }, detailFromLocation, ({ annotations, baseline, detail, frames }) => {
    resetFrameCache();
    state.detail = detail;
    state.frames = frames;
    state.annotations = annotations;
    state.baseline = baseline;
    state.selected = 0;
    byId("detail-kicker").textContent = `${detail.pack.id} · ${detail.status} · ${detail.id}`;
    byId("detail-title").textContent = detail.title;
    byId("detail-description").textContent = detail.description;
    byId("frame-scrubber").max = String(frames.frames.length - 1);
    const real = detail.pack.id === "real-video-v2";
    const mot = detail.pack.id === "motchallenge-v1";
    byId("objects-overlay-label").querySelector("span").textContent = real ? "Tracked objects" : "Targets";
    byId("ignores-overlay-label").hidden = real;
    byId("region-overlay-label").hidden = real;
    byId("frame-stage").hidden = mot;
    byId("viewer-controls").hidden = mot;
    byId("overlay-controls").hidden = mot;
    byId("frame-inspector").hidden = mot;
    const video = byId("scenario-video");
    video.pause();
    video.hidden = !mot;
    video.removeAttribute("src");
    video.removeAttribute("poster");
    if (mot) {
      video.src = detail.media.viewer_derivative.url;
      video.poster = detail.media.visual_audit_overview.url;
      video.load();
    }
    byId("overlay-disclosure").textContent = mot
      ? "This deterministic public derivative plays ordered JPEGs at publisher-declared 25/30 FPS. It does not represent original container timestamps. Exact frame hashes and full normalized annotations are linked below."
      : real
      ? "Public whole-scene overlays are human inspection aids. Systems receive only progressive frames and timestamps—never these boxes, identities, annotations, or future frames."
      : "Public overlays are human inspection aids and are never sent to submitted systems.";
    byId("scenario-detail").hidden = false;
    renderDetailFacts();
    if (!mot) void showFrame(0, false);
    setCatalogStatus(`${state.catalog.scenario_count} current scenarios are public.`);
    byId("scenario-detail").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  });
}

function detailFromLocation() {
  return new URLSearchParams(location.search).get("scenario");
}

async function init() {
  try {
    state.catalog = await fetchJson("/scenario-catalog/v1/catalog.json");
    if (state.catalog.scenario_count !== 26 || !state.catalog.all_current_scenarios_public) throw new Error("The catalog completeness assertion failed.");
    byId("scenario-count").textContent = state.catalog.scenario_count;
    renderCatalog();
    const selected = detailFromLocation();
    if (selected) await loadDetail(selected);
  } catch (error) {
    setCatalogStatus(error.message, true);
  }
}

byId("scenario-search").addEventListener("input", renderCatalog);
byId("pack-filter").addEventListener("change", renderCatalog);
byId("previous-frame").addEventListener("click", () => void showFrame(state.selected - 1));
byId("next-frame").addEventListener("click", () => void showFrame(state.selected + 1));
byId("play-pause").addEventListener("click", togglePlayback);
byId("playback-speed").addEventListener("change", (event) => {
  const now = performance.now();
  if (state.playing && state.playbackAnchorMs !== null) {
    state.playbackAnchorSourceNs += (now - state.playbackAnchorMs) * 1_000_000 * state.playbackSpeed;
    state.playbackAnchorMs = now;
  }
  state.playbackSpeed = Number(event.currentTarget.value);
  byId("viewer-announcement").textContent = `Playback speed ${state.playbackSpeed} times.`;
});
byId("frame-scrubber").addEventListener("input", (event) => void showFrame(Number(event.currentTarget.value)));
for (const toggle of document.querySelectorAll("[data-overlay]")) toggle.addEventListener("change", renderOverlay);
byId("frame-viewer").addEventListener("keydown", (event) => {
  if (!state.frames || ["INPUT", "BUTTON"].includes(event.target.tagName)) return;
  if (event.key === " ") { event.preventDefault(); togglePlayback(); }
  if (event.key === "ArrowLeft") { event.preventDefault(); void showFrame(state.selected - 1); }
  if (event.key === "ArrowRight") { event.preventDefault(); void showFrame(state.selected + 1); }
  if (event.key === "Home") { event.preventDefault(); void showFrame(0); }
  if (event.key === "End") { event.preventDefault(); void showFrame(state.frames.frames.length - 1); }
});
window.addEventListener("popstate", () => {
  const selected = detailFromLocation();
  if (selected) void loadDetail(selected).catch((error) => setCatalogStatus(error.message, true));
  else {
    detailLoader.cancel();
    stopPlayback();
    byId("scenario-detail").hidden = true;
  }
});

void init();
