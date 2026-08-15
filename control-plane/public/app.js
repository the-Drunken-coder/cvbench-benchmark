const page = ({ "/": "datasets", "/datasets/": "datasets", "/runs/": "runs", "/submit/": "submit", "/docs/": "docs" })[window.location.pathname] ?? "datasets";
for (const view of document.querySelectorAll("[data-view]")) view.hidden = view.dataset.view !== page;
document.querySelector(`[data-nav="${page}"]`)?.setAttribute("aria-current", "page");
document.title = `${page[0].toUpperCase()}${page.slice(1)} · CVBench`;

const legacySubmission = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("submission");
if (legacySubmission) window.location.replace(`/results/?submission=${encodeURIComponent(legacySubmission)}`);

const byId = (id) => document.getElementById(id);
const setText = (id, value) => { byId(id).textContent = value; };
const titleCase = (value) => value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
const SVG_NS = "http://www.w3.org/2000/svg";
let activeTrackingClip = null;
let activeTrackingBoxes = [];
const trackingCache = new Map();

function identityCell(title, id, select) {
  const cell = document.createElement("td");
  const button = document.createElement("button");
  const detail = document.createElement("small");
  button.type = "button";
  button.textContent = title;
  button.addEventListener("click", select);
  detail.textContent = id;
  cell.append(button, detail);
  return cell;
}

function textCell(value) {
  const cell = document.createElement("td");
  cell.textContent = value;
  return cell;
}

function resetVideo(video) {
  video.pause();
  video.removeAttribute("src");
  video.load();
}

function renderTrackingBoxes() {
  const overlay = byId("clip-tracking-overlay");
  const video = byId("clip-video");
  overlay.replaceChildren();
  if (!activeTrackingBoxes.length || !byId("clip-box-toggle").checked || video.hidden) return;

  const now = video.currentTime * 1_000_000_000;
  let nearestTimestamp = null;
  let nearestDistance = Infinity;
  for (const box of activeTrackingBoxes) {
    const distance = Math.abs(box.timestampNs - now);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestTimestamp = box.timestampNs;
    }
  }
  const frameIntervalNs = 1_000_000_000
    * activeTrackingClip.media.fpsDenominator
    / activeTrackingClip.media.fpsNumerator;
  if (nearestDistance > frameIntervalNs * 0.75) return;

  const { width, height } = activeTrackingClip.media;
  overlay.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const labelSize = Math.max(18, height / 40);
  for (const box of activeTrackingBoxes) {
    if (box.timestampNs !== nearestTimestamp) continue;
    const [x1, y1, x2, y2] = box.bbox;
    const rectangle = document.createElementNS(SVG_NS, "rect");
    rectangle.setAttribute("class", "clip-tracking-box");
    rectangle.setAttribute("x", x1);
    rectangle.setAttribute("y", y1);
    rectangle.setAttribute("width", x2 - x1);
    rectangle.setAttribute("height", y2 - y1);
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("class", "clip-tracking-label");
    label.setAttribute("x", x1);
    label.setAttribute("y", Math.max(labelSize, y1 - labelSize / 3));
    label.setAttribute("font-size", labelSize);
    label.textContent = `${box.trackId} ${Math.round(box.confidence * 100)}%`;
    overlay.append(rectangle, label);
  }
}

async function loadTrackingBoxes(clip) {
  if (!clip.tracking) return [];
  let request = trackingCache.get(clip.tracking.url);
  if (!request) {
    request = fetch(clip.tracking.url).then(async (response) => {
      if (!response.ok) throw new Error(`Tracking boxes returned ${response.status}.`);
      const document = await response.json();
      if (document.schemaVersion !== "cvbench.browser-boxes/v2" || !Array.isArray(document.boxes)) {
        throw new Error("Tracking boxes use an unsupported format.");
      }
      return document.boxes.map((row) => {
        if (!Array.isArray(row) || row.length !== 8) throw new Error("Tracking box row is invalid.");
        const [timestampNs, x1, y1, x2, y2, trackId, classId, confidence] = row;
        return { timestampNs, bbox: [x1, y1, x2, y2], trackId, classId, confidence };
      });
    });
    trackingCache.set(clip.tracking.url, request);
    request.catch(() => trackingCache.delete(clip.tracking.url));
  }
  return request;
}

function selectTrackingClip(clip) {
  if (activeTrackingClip === clip) return;
  activeTrackingClip = clip;
  activeTrackingBoxes = [];
  byId("clip-box-toggle-label").hidden = true;
  renderTrackingBoxes();
  if (!clip.preview || !clip.tracking) return;
  loadTrackingBoxes(clip).then((boxes) => {
    if (activeTrackingClip !== clip) return;
    activeTrackingBoxes = boxes;
    byId("clip-box-toggle-label").hidden = boxes.length === 0;
    renderTrackingBoxes();
  }).catch(() => {
    if (activeTrackingClip === clip) byId("clip-box-toggle-label").hidden = true;
  });
}

if (page === "datasets") {
  const video = byId("clip-video");
  const toggle = byId("clip-box-toggle");
  let playbackGeneration = 0;
  video.addEventListener("loadedmetadata", () => {
    if (!video.getAttribute("src")) return;
    const duration = Number.isFinite(video.duration) ? `${Math.round(video.duration)} seconds` : "Ready";
    setText("clip-media-status", `Browser preview · ${duration}`);
    renderTrackingBoxes();
  });
  video.addEventListener("timeupdate", renderTrackingBoxes);
  video.addEventListener("seeked", renderTrackingBoxes);
  video.addEventListener("play", () => {
    if (!("requestVideoFrameCallback" in video)) return;
    const generation = ++playbackGeneration;
    const renderFrame = () => {
      if (generation !== playbackGeneration) return;
      renderTrackingBoxes();
      if (!video.paused && !video.ended) video.requestVideoFrameCallback(renderFrame);
    };
    video.requestVideoFrameCallback(renderFrame);
  });
  video.addEventListener("pause", () => { playbackGeneration += 1; });
  video.addEventListener("ended", () => { playbackGeneration += 1; });
  video.addEventListener("error", () => {
    if (!video.getAttribute("src")) return;
    setText("clip-media-status", "Preview unavailable. Open the original source below.");
    activeTrackingBoxes = [];
    byId("clip-box-toggle-label").hidden = true;
    renderTrackingBoxes();
  });
  toggle.addEventListener("change", renderTrackingBoxes);
  loadDatasets().catch((error) => {
    setText("dataset-status", error instanceof Error ? error.message : "Dataset catalog could not be loaded.");
    byId("dataset-status").classList.add("error");
  });
}

async function loadDatasets() {
  const response = await fetch("/dataset-catalog/v1/catalog.json");
  if (!response.ok) {
    setText("dataset-status", `Dataset catalog returned ${response.status}.`);
    byId("dataset-status").classList.add("error");
    return;
  }
  const catalog = await response.json();
  let selectedDataset = catalog.datasets.find((dataset) => dataset.collection === "dataset") ?? catalog.datasets[0];
  let selectedClip = selectedDataset?.clips[0];
  const repositoryBase = `${catalog.repository.url}/tree/${catalog.repository.revision}`;
  const revisionLink = byId("dataset-revision");
  revisionLink.href = repositoryBase;
  revisionLink.textContent = `Synced from ${catalog.repository.name} · ${catalog.repository.revision.slice(0, 7)}`;

  function render() {
    const query = byId("dataset-search").value.trim().toLowerCase();
    const role = byId("dataset-role").value;
    const state = byId("dataset-state").value;
    const datasets = catalog.datasets.filter((dataset) => {
      const searchable = [dataset.id, dataset.title, dataset.description, ...dataset.clips.flatMap((clip) => [clip.id, clip.title, clip.sourceTitle])].join(" ").toLowerCase();
      return (role === "all" || dataset.dataRole === role) && (state === "all" || dataset.state === state) && (!query || searchable.includes(query));
    });
    if (!datasets.includes(selectedDataset)) {
      selectedDataset = datasets[0];
      selectedClip = selectedDataset?.clips[0];
    }
    byId("dataset-rows").replaceChildren(...datasets.map((dataset) => {
      const row = document.createElement("tr");
      if (dataset === selectedDataset) row.className = "is-selected";
      row.append(
        identityCell(dataset.title, dataset.id, () => { selectedDataset = dataset; selectedClip = dataset.clips[0]; render(); }),
        textCell(dataset.version), textCell(titleCase(dataset.state)), textCell(titleCase(dataset.dataRole)),
        textCell(String(dataset.clips.length)), textCell(dataset.evaluationEligible ? "Eligible" : "Excluded"),
      );
      return row;
    }));
    setText("dataset-status", datasets.length ? `${datasets.length} datasets` : "No datasets match those filters.");
    renderDataset(repositoryBase, selectedDataset, selectedClip, (clip) => { selectedClip = clip; render(); });
  }

  byId("dataset-filters").addEventListener("input", render);
  render();
}

function renderDataset(repositoryBase, dataset, selectedClip, selectClip) {
  const detail = byId("dataset-detail");
  detail.hidden = !dataset;
  if (!dataset) {
    activeTrackingClip = null;
    activeTrackingBoxes = [];
    resetVideo(byId("clip-video"));
    byId("clip-video-stage").hidden = true;
    byId("clip-box-toggle-label").hidden = true;
    renderTrackingBoxes();
    return;
  }
  const annotations = dataset.clips.reduce((total, clip) => total + clip.annotationRows, 0);
  const approvals = dataset.clips.reduce((total, clip) => total + clip.humanApprovals, 0);
  setText("dataset-path", `${dataset.id} / ${dataset.version}`);
  setText("dataset-title", dataset.title);
  setText("dataset-description", dataset.description);
  setText("dataset-annotations", annotations.toLocaleString());
  setText("dataset-approvals", approvals.toLocaleString());
  setText("dataset-scope", titleCase(dataset.annotationScope));
  setText("dataset-required-approvals", `${dataset.requiredIndependentApprovals} per clip`);
  setText("clip-count", `${dataset.clips.length} records`);
  byId("dataset-source").href = `${repositoryBase}/${dataset.path}`;
  byId("clip-rows").replaceChildren(...dataset.clips.map((clip) => {
    const row = document.createElement("tr");
    if (clip === selectedClip) row.className = "is-selected";
    row.append(identityCell(clip.title, clip.id, () => selectClip(clip)), textCell(clip.media.frameCount.toLocaleString()), textCell(clip.annotationRows.toLocaleString()), textCell(`${clip.humanApprovals} / ${dataset.requiredIndependentApprovals}`));
    return row;
  }));
  renderClip(repositoryBase, dataset, selectedClip);
}

function renderClip(repositoryBase, dataset, clip) {
  byId("clip-detail").hidden = !clip;
  if (!clip) return;
  selectTrackingClip(clip);
  setText("clip-title", clip.title);
  setText("clip-source-title", clip.sourceTitle);
  setText("clip-frames", clip.media.frameCount.toLocaleString());
  setText("clip-resolution", `${clip.media.width} × ${clip.media.height}`);
  setText("clip-fps", String(Number((clip.media.fpsNumerator / clip.media.fpsDenominator).toFixed(3))));
  setText("clip-model", clip.model ?? "None");
  setText("clip-sha", `${clip.sourceSha256.slice(0, 10)}…${clip.sourceSha256.slice(-6)}`);
  const video = byId("clip-video");
  if (clip.preview) {
    video.hidden = false;
    byId("clip-video-stage").hidden = false;
    if (video.getAttribute("src") !== clip.preview.url) {
      video.pause();
      video.src = clip.preview.url;
      setText("clip-media-status", "Loading browser preview.");
      video.load();
    }
  } else {
    resetVideo(video);
    video.hidden = true;
    byId("clip-video-stage").hidden = true;
    setText("clip-media-status", "No browser preview is published for this clip.");
  }
  renderTrackingBoxes();
  const license = byId("clip-license");
  license.href = clip.license.url;
  license.textContent = clip.license.spdx;
  byId("clip-files").href = `${repositoryBase}/${dataset.path}/${clip.path}`;
  byId("clip-original").href = clip.sourceUri;
}

if (page === "runs") {
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
  byId("run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const id = byId("submission-id").value.trim();
    if (!uuid.test(id)) return setText("run-error", "Enter the complete submission UUID.");
    window.location.assign(`/results/?submission=${encodeURIComponent(id)}`);
  });
}

if (page === "submit") {
  const command = "cvbench submit . --wait --json";
  byId("copy-command").addEventListener("click", async (event) => {
    await navigator.clipboard.writeText(command);
    event.currentTarget.textContent = "Copied";
  });
  const form = byId("registry-form");
  let idempotencyKey = `web-${crypto.randomUUID()}`;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const image = data.get("image").trim();
    let argv;
    try { argv = JSON.parse(data.get("argv")); } catch { argv = null; }
    if (!/^(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?\/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[a-f0-9]{64}$/.test(image)) return submitError("Use a lowercase registry/repository@sha256 digest. Mutable tags cannot be queued.");
    if (!Array.isArray(argv) || !argv.length || argv.some((item) => typeof item !== "string" || !item.trim())) return submitError("Command arguments must be a non-empty JSON string array.");
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    byId("submit-status").className = "";
    setText("submit-status", "Queueing the immutable image.");
    try {
      const response = await fetch("/api/v1/submissions", {
        method: "POST",
        headers: { authorization: `Bearer ${data.get("api_key")}`, "content-type": "application/json", "idempotency-key": idempotencyKey },
        body: JSON.stringify({ image, argv: argv.map((item) => item.trim()), name: data.get("name").trim(), model_version: data.get("model_version").trim() }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body?.error?.message ?? "The submission could not be queued.");
      if (!body?.id) throw new Error("The queue returned an invalid submission record.");
      form.elements.api_key.value = "";
      idempotencyKey = `web-${crypto.randomUUID()}`;
      window.location.assign(`/results/?submission=${encodeURIComponent(body.id)}`);
    } catch (error) {
      submitError(error instanceof Error ? error.message : "The submission could not be queued.");
      submit.disabled = false;
    }
  });
}

function submitError(message) {
  setText("submit-status", message);
  byId("submit-status").className = "error";
}
