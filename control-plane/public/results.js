const terminalStatuses = new Set(["succeeded", "failed"]);
const resultScenarios = [
  { id: "rvmot-a1c9", label: "Loading + occlusion" },
  { id: "rvmot-b7e2", label: "Close handoff" },
  { id: "rvmot-c4f6", label: "Wide parking view" },
];

let statusGeneration = 0;
let statusPollTimer = null;
let playbackGeneration = 0;
let playbackTimer = null;
let playback = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function formatPercent(value) {
  return value == null ? "Unavailable" : `${(Number(value) * 100).toFixed(1)}%`;
}

function formatNumber(value, maximumFractionDigits = 2) {
  return value == null
    ? "Unavailable"
    : Number(value).toLocaleString(undefined, { maximumFractionDigits });
}

function formatMilliseconds(value) {
  return value == null ? "Unavailable" : `${formatNumber(value, 2)} ms`;
}

function formatSeconds(value) {
  return value == null ? "Unavailable" : `${formatNumber(value, 2)} s`;
}

function formatBytes(value) {
  return value == null ? "Unavailable" : `${formatNumber(Number(value) / (1024 * 1024), 1)} MiB`;
}

function formatMultiplier(value) {
  return value == null ? "Unavailable" : `${formatNumber(value, 3)}×`;
}

function formatBoolean(value) {
  return value == null ? "Unavailable" : value ? "Yes" : "No";
}

function formatTimestamp(value) {
  if (!value) return "Waiting";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function resultMetric(label, value, detail) {
  const item = element("div", undefined, "result-metric");
  item.append(element("span", label), element("strong", value));
  if (detail) item.append(element("small", detail));
  return item;
}

function metricGroup(title, metrics) {
  const group = element("section", undefined, "metric-group");
  group.append(element("h4", title));
  const grid = element("div", undefined, "result-metric-grid");
  for (const metric of metrics) grid.append(resultMetric(...metric));
  group.append(grid);
  return group;
}

function renderTimeline(body) {
  const timeline = element("ol", undefined, "run-timeline");
  for (const [label, value] of [
    ["Queued", body.created_at],
    ["Runner started", body.started_at],
    [body.status === "failed" ? "Failed" : "Completed", body.completed_at],
  ]) {
    const item = element("li");
    if (value) item.classList.add("complete");
    item.append(element("span"), element("strong", label), element("small", formatTimestamp(value)));
    timeline.append(item);
  }
  return timeline;
}

function pausePlayback() {
  if (playback) playback.playing = false;
  clearTimeout(playbackTimer);
  playbackTimer = null;
  if (playback?.playButton) {
    playback.playButton.textContent = "Play";
    playback.playButton.setAttribute("aria-label", "Play benchmark footage");
  }
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Benchmark media request failed (${response.status}).`);
  return response.json();
}

function renderGroundTruth(objects) {
  const overlay = playback.overlay;
  overlay.replaceChildren();
  overlay.setAttribute("viewBox", `0 0 ${playback.width} ${playback.height}`);
  for (const object of objects || []) {
    if (!object.on_screen || !Array.isArray(object.bbox_xyxy)) continue;
    const [x1, y1, x2, y2] = object.bbox_xyxy;
    overlay.append(svgElement("rect", {
      x: x1,
      y: y1,
      width: Math.max(0, x2 - x1),
      height: Math.max(0, y2 - y1),
      class: "result-ground-truth-box",
    }));
    const label = svgElement("text", {
      x: Math.max(4, x1),
      y: Math.max(16, y1 - 5),
      class: "result-ground-truth-label",
    });
    label.textContent = `${object.class_id} · ${object.target_id}`;
    overlay.append(label);
  }
}

async function showPlaybackFrame(index, generation = playbackGeneration) {
  if (!playback || generation !== playbackGeneration) return;
  const bounded = Math.max(0, Math.min(index, playback.frames.length - 1));
  const frame = playback.frames[bounded];
  const image = new Image();
  image.decoding = "async";
  image.src = frame.media.url;
  try {
    await image.decode();
  } catch {
    if (generation !== playbackGeneration) return;
    playback.mediaState.textContent = "This exact benchmark frame could not be decoded.";
    playback.mediaState.hidden = false;
    pausePlayback();
    return;
  }
  if (!playback || generation !== playbackGeneration) return;

  playback.index = bounded;
  playback.image.src = frame.media.url;
  playback.image.alt = `Exact benchmark frame ${bounded + 1} of ${playback.frames.length}`;
  playback.scrubber.value = String(bounded);
  playback.position.textContent = `${bounded + 1} / ${playback.frames.length}`;
  playback.time.textContent = `${(frame.source_timestamp_ns / 1_000_000_000).toFixed(3)} s`;
  playback.mediaState.hidden = true;
  const annotationFrame = playback.annotations.frames.find((item) => item.frame_index === frame.frame_index);
  renderGroundTruth(annotationFrame?.objects || []);

  for (const upcoming of playback.frames.slice(bounded + 1, bounded + 4)) {
    const preload = new Image();
    preload.src = upcoming.media.url;
  }
}

async function advancePlayback(generation) {
  if (!playback?.playing || generation !== playbackGeneration) return;
  const next = playback.index + 1;
  if (next >= playback.frames.length) {
    pausePlayback();
    return;
  }
  await showPlaybackFrame(next, generation);
  if (!playback?.playing || generation !== playbackGeneration) return;
  playbackTimer = setTimeout(
    () => advancePlayback(generation),
    1000 / (playback.fps * playback.speed),
  );
}

async function loadPlaybackScenario(id, view) {
  pausePlayback();
  playbackGeneration += 1;
  const generation = playbackGeneration;
  view.mediaState.hidden = false;
  view.mediaState.textContent = "Loading exact benchmark frames…";
  for (const button of view.scenarioButtons) {
    const selected = button.dataset.scenario === id;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  try {
    const detail = await fetchJson(`/scenario-catalog/v1/scenarios/${encodeURIComponent(id)}.json`);
    const [frames, annotations] = await Promise.all([
      fetchJson(detail.media.frame_manifest.url),
      fetchJson(detail.annotations.annotation_manifest.url),
    ]);
    if (generation !== playbackGeneration) return;
    playback = {
      ...view,
      scenarioId: id,
      frames: frames.frames,
      annotations,
      width: detail.media.width,
      height: detail.media.height,
      fps: detail.media.fps || 10,
      index: 0,
      speed: Number(view.speed.value),
      playing: false,
    };
    view.image.width = detail.media.width;
    view.image.height = detail.media.height;
    view.scrubber.max = String(frames.frames.length - 1);
    view.title.textContent = detail.title;
    view.openScenario.href = `/scenarios/?scenario=${encodeURIComponent(id)}`;
    await showPlaybackFrame(0, generation);
  } catch (error) {
    if (generation !== playbackGeneration) return;
    view.mediaState.textContent = error.message;
    view.mediaState.hidden = false;
  }
}

function renderPlayback() {
  const theater = element("section", undefined, "result-theater");
  const heading = element("div", undefined, "result-theater-heading");
  const headingCopy = element("div");
  headingCopy.append(
    element("p", "Benchmark playback", "kicker"),
    element("h3", "See the exact frames behind the score."),
  );
  const disclosure = element(
    "p",
    "Crisp public benchmark footage with ground-truth boxes. These overlays are not the submitted system’s predictions, which are not exposed by the public result API.",
    "playback-disclosure",
  );
  heading.append(headingCopy, disclosure);

  const reel = element("div", undefined, "result-reel");
  const scenarioButtons = resultScenarios.map((scenario) => {
    const button = element("button", scenario.label);
    button.type = "button";
    button.dataset.scenario = scenario.id;
    button.setAttribute("aria-pressed", "false");
    reel.append(button);
    return button;
  });

  const viewer = element("div", undefined, "result-video");
  const stage = element("div", undefined, "result-video-stage");
  const image = document.createElement("img");
  image.alt = "";
  const overlay = svgElement("svg", { "aria-hidden": "true" });
  const mediaState = element("p", "Loading exact benchmark frames…", "result-media-state");
  stage.append(image, overlay, mediaState);

  const controls = element("div", undefined, "result-video-controls");
  const previous = element("button", "←");
  previous.type = "button";
  previous.setAttribute("aria-label", "Previous benchmark frame");
  const playButton = element("button", "Play");
  playButton.type = "button";
  playButton.setAttribute("aria-label", "Play benchmark footage");
  const next = element("button", "→");
  next.type = "button";
  next.setAttribute("aria-label", "Next benchmark frame");
  const scrubber = document.createElement("input");
  scrubber.type = "range";
  scrubber.min = "0";
  scrubber.max = "0";
  scrubber.value = "0";
  scrubber.setAttribute("aria-label", "Benchmark frame");
  const position = element("output", "0 / 0");
  const time = element("output", "0.000 s");
  const speedLabel = element("label", "Speed ");
  const speed = document.createElement("select");
  speed.setAttribute("aria-label", "Playback speed");
  for (const value of [0.5, 1, 2]) {
    const option = element("option", `${value}×`);
    option.value = String(value);
    if (value === 1) option.selected = true;
    speed.append(option);
  }
  speedLabel.append(speed);
  controls.append(previous, playButton, next, scrubber, position, time, speedLabel);

  const footer = element("div", undefined, "result-video-footer");
  const legend = element("p", "▣ Public exhaustive ground truth", "ground-truth-legend");
  const openScenario = element("a", "Open full frame inspector →");
  footer.append(legend, openScenario);
  viewer.append(stage, controls, footer);
  theater.append(heading, reel, viewer);

  const view = {
    title: headingCopy.querySelector("h3"),
    scenarioButtons,
    image,
    overlay,
    mediaState,
    previous,
    playButton,
    next,
    scrubber,
    position,
    time,
    speed,
    openScenario,
  };
  for (const button of scenarioButtons) {
    button.addEventListener("click", () => loadPlaybackScenario(button.dataset.scenario, view));
  }
  previous.addEventListener("click", () => {
    if (!playback) return;
    pausePlayback();
    showPlaybackFrame(playback.index - 1);
  });
  next.addEventListener("click", () => {
    if (!playback) return;
    pausePlayback();
    showPlaybackFrame(playback.index + 1);
  });
  playButton.addEventListener("click", async () => {
    if (!playback) return;
    if (playback.playing) {
      pausePlayback();
      return;
    }
    if (playback.index >= playback.frames.length - 1) await showPlaybackFrame(0);
    playback.playing = true;
    playButton.textContent = "Pause";
    playButton.setAttribute("aria-label", "Pause benchmark footage");
    advancePlayback(playbackGeneration);
  });
  scrubber.addEventListener("input", () => {
    pausePlayback();
    showPlaybackFrame(Number(scrubber.value));
  });
  speed.addEventListener("change", () => {
    if (playback) playback.speed = Number(speed.value);
  });
  loadPlaybackScenario(resultScenarios[0].id, view);
  return theater;
}

function renderFindings(findings) {
  const section = element("section", undefined, "result-findings");
  const heading = element("div", undefined, "subsection-heading");
  heading.append(element("p", "Findings", "kicker"), element("h3", findings.length ? "What deserves attention." : "No structured findings."));
  section.append(heading);
  if (!findings.length) return section;
  const list = element("div", undefined, "finding-list");
  for (const finding of findings) {
    const card = element("article", undefined, `finding-card severity-${finding.severity || "unknown"}`);
    const meta = element("p", `${finding.severity || "unrated"} · ${finding.category || "general"}`);
    card.append(meta, element("h4", finding.finding_id || "Finding"), element("p", finding.statement || "No public statement."));
    list.append(card);
  }
  section.append(list);
  return section;
}

function resultShareUrl(id) {
  const url = new URL("/results/", location.origin);
  url.searchParams.set("submission", id);
  return url.toString();
}

function renderSubmission(body) {
  pausePlayback();
  playbackGeneration += 1;
  const output = document.querySelector("#status-output");
  output.replaceChildren();

  const header = element("article", undefined, "result-header");
  const identity = element("div", undefined, "result-identity");
  const statusLine = element("p", undefined, "result-status-line");
  const pill = element("span", body.status, `status-pill status-${body.status}`);
  statusLine.append(pill, ` Attempt ${body.attempt}`);
  identity.append(
    statusLine,
    element("h3", body.model.name),
    element("p", `System version ${body.model.version} · ${body.id}`),
  );
  const actions = element("div", undefined, "result-actions");
  const share = element("button", "Copy share link", "button secondary");
  share.type = "button";
  share.addEventListener("click", async () => {
    await navigator.clipboard.writeText(resultShareUrl(body.id));
    share.textContent = "Link copied";
    setTimeout(() => { share.textContent = "Copy share link"; }, 1200);
  });
  const api = element("a", "Open JSON", "button secondary");
  api.href = `/api/v1/submissions/${encodeURIComponent(body.id)}`;
  actions.append(share, api);
  header.append(identity, actions);

  const timeline = renderTimeline(body);
  output.append(header, timeline);

  const scores = body.result?.scores;
  if (scores) {
    const eligibility = element("section", undefined, "result-eligibility");
    const eligibilityCopy = element("div");
    eligibilityCopy.append(
      element("p", "Leaderboard class", "kicker"),
      element("h3", scores.leaderboard_class || "Unclassified"),
      element(
        "p",
        scores.leaderboard_eligible
          ? "Eligible result with complete trusted-runner accounting."
          : "This run is not eligible for leaderboard placement.",
      ),
    );
    eligibility.append(
      eligibilityCopy,
      resultMetric("Replay", scores.replay_profile != null && scores.replay_rate != null
        ? `${scores.replay_profile} @ ${scores.replay_rate}×`
        : "Unavailable"),
      resultMetric("Accounting", formatBoolean(scores.accounting_complete)),
    );

    const groups = element("div", undefined, "metric-groups");
    groups.append(
      metricGroup("Accuracy", [
        ["Acquisition", formatPercent(scores.acquisition_rate)],
        ["Observed coverage", formatPercent(scores.observed_coverage)],
        ["Mean IoU", formatPercent(scores.mean_iou)],
        ["False track births", formatNumber(scores.false_track_births, 0)],
      ]),
      metricGroup("Identity", [
        ["ID switches", formatNumber(scores.id_switches, 0)],
        ["Same-ID reacquisition", formatPercent(scores.reacquisition_same_id_rate)],
        ["Continuity coverage", formatPercent(scores.continuity_coverage)],
        ["Output records", formatNumber(scores.sample_counts?.output_records, 0)],
      ]),
      metricGroup("Timing", [
        ["Median latency", formatMilliseconds(scores.latency_p50_ms)],
        ["P99 latency", formatMilliseconds(scores.latency_p99_ms)],
        ["Processing P95", formatMilliseconds(scores.processing_latency_p95_ms)],
        ["Real-time factor", formatMultiplier(scores.real_time_factor)],
      ]),
      metricGroup("Resources", [
        ["CPU s/source s", formatNumber(scores.cpu_seconds_per_native_source_second, 3)],
        ["Average CPU", scores.average_cpu_percent == null ? "Unavailable" : `${formatNumber(scores.average_cpu_percent, 1)}%`],
        ["Peak RAM", formatBytes(scores.peak_ram_bytes)],
        ["Wall time", formatSeconds(scores.wall_seconds)],
      ]),
    );

    output.append(eligibility, renderPlayback(), groups, renderFindings(body.result.findings || []));
  } else {
    const pending = element("section", undefined, "result-pending");
    pending.append(
      element("p", body.status === "failed" ? "Run failed" : "Live queue", "kicker"),
      element("h3", body.status === "failed" ? (body.error || "The runner did not complete this submission.") : "Waiting for a terminal result…"),
      element("p", terminalStatuses.has(body.status)
        ? "The public record is terminal."
        : "This page refreshes automatically while the job is queued or running."),
    );
    output.append(pending);
  }

  const raw = element("details", undefined, "raw-result");
  raw.append(element("summary", "Machine-readable public record"));
  const pre = element("pre", JSON.stringify(body, null, 2));
  raw.append(pre);
  output.append(raw);
}

async function fetchSubmission(id, generation, poll) {
  if (generation !== statusGeneration) return;
  try {
    const response = await fetch(`/api/v1/submissions/${encodeURIComponent(id)}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Could not load submission.");
    if (generation !== statusGeneration) return;
    renderSubmission(body);
    const url = new URL("/results/", location.origin);
    url.searchParams.set("submission", id);
    history.replaceState(null, "", url);
    if (poll && !terminalStatuses.has(body.status)) {
      statusPollTimer = setTimeout(() => fetchSubmission(id, generation, poll), 5000);
    }
  } catch (error) {
    if (generation !== statusGeneration) return;
    document.querySelector("#status-output").replaceChildren(element("p", error.message, "error"));
  }
}

function trackSubmission(id, poll) {
  statusGeneration += 1;
  const generation = statusGeneration;
  clearTimeout(statusPollTimer);
  pausePlayback();
  playbackGeneration += 1;
  const output = document.querySelector("#status-output");
  output.replaceChildren(element("p", "Loading public result studio…"));
  fetchSubmission(id, generation, poll);
}

document.querySelector("#status-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const id = new FormData(event.currentTarget).get("submission").trim();
  trackSubmission(id, true);
});

const queryId = new URLSearchParams(location.search).get("submission");
if (queryId) {
  document.querySelector("#submission-id").value = queryId;
  trackSubmission(queryId, true);
}
