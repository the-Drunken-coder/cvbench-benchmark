const terminalStatuses = new Set(["succeeded", "failed"]);
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

function scenarioLabel(id) {
  return {
    "synthetic-acquisition": "Initial acquisition",
    "synthetic-false-detection": "False detection rejection",
    "synthetic-multi-target-identity": "Multi-target identity",
    "synthetic-multi-target-pair": "Paired targets",
    "synthetic-occlusion-gap-100ms": "Occlusion · 100 ms",
    "synthetic-occlusion-gap-250ms": "Occlusion · 250 ms",
    "synthetic-occlusion-gap-500ms": "Occlusion · 500 ms",
    "synthetic-occlusion-gap-1000ms": "Occlusion · 1 second",
    "synthetic-occlusion-gap-2000ms": "Occlusion · 2 seconds",
    "synthetic-occlusion-reacquisition": "Occlusion reacquisition",
    "synthetic-resource-stress": "Resource stress",
    "synthetic-track-id-churn": "Track identity stability",
    "synthetic-visible-retention": "Visible target retention",
    "rvmot-a1c9": "Loading + occlusion",
    "rvmot-b7e2": "Close handoff",
    "rvmot-c4f6": "Wide parking view",
  }[id] || id.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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

function renderLiveProgress(body) {
  const progress = body.progress || {};
  const total = Number(progress.total || body.benchmark?.scenario_count || 0);
  const completed = Math.min(total, Math.max(0, Number(progress.completed || 0)));
  const fraction = total ? completed / total : 0;
  const section = element("section", undefined, "run-progress");
  const copy = element("div");
  copy.append(
    element("p", terminalStatuses.has(body.status) ? "Run record" : "Live trusted runner", "kicker"),
    element("h3", progress.message || (body.status === "queued" ? "Waiting for a trusted runner." : body.status)),
    element("p", `${progress.stage || body.status} · ${completed} of ${total || "?"} scenarios published`, "run-progress-detail"),
  );
  const meter = element("div", undefined, "run-progress-meter");
  meter.setAttribute("role", "progressbar");
  meter.setAttribute("aria-label", "Benchmark run progress");
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", String(total || 1));
  meter.setAttribute("aria-valuenow", String(completed));
  const fill = element("span");
  fill.style.width = `${Math.round(fraction * 100)}%`;
  meter.append(fill);
  section.append(copy, meter);
  return section;
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
  const overlay = playback.sourceOverlay;
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

function renderModelPredictions(objects) {
  const overlay = playback.modelOverlay;
  overlay.replaceChildren();
  overlay.setAttribute("viewBox", `0 0 ${playback.width} ${playback.height}`);
  for (const object of objects || []) {
    if (!Array.isArray(object.bbox_xyxy)) continue;
    const [x1, y1, x2, y2] = object.bbox_xyxy;
    overlay.append(svgElement("rect", {
      x: x1,
      y: y1,
      width: Math.max(0, x2 - x1),
      height: Math.max(0, y2 - y1),
      class: "result-model-box",
    }));
    const label = svgElement("text", {
      x: Math.max(4, x1),
      y: Math.max(16, y1 - 5),
      class: "result-model-label",
    });
    label.textContent = `${object.class_id} · ${object.track_label}`;
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
  for (const [imageElement, label] of [
    [playback.sourceImage, "ground-truth"],
    [playback.modelImage, "model-prediction"],
  ]) {
    imageElement.src = frame.media.url;
    imageElement.alt = `Exact benchmark frame ${bounded + 1} of ${playback.frames.length} with ${label} overlay`;
  }
  playback.scrubber.value = String(bounded);
  playback.position.textContent = `${bounded + 1} / ${playback.frames.length}`;
  playback.time.textContent = `${(frame.source_timestamp_ns / 1_000_000_000).toFixed(3)} s`;
  playback.mediaState.hidden = true;
  const annotationFrame = playback.annotations.frames.find((item) => item.frame_index === frame.frame_index);
  renderGroundTruth(annotationFrame?.objects || []);
  if (playback.modelArtifact?.state === "complete") {
    const modelFrame = playback.modelArtifact.frames.find((item) => item.frame_index === frame.frame_index);
    renderModelPredictions(modelFrame?.objects || []);
    playback.modelState.hidden = Boolean(modelFrame?.objects?.length);
    playback.modelState.textContent = "No model tracks emitted for this frame.";
  } else {
    renderModelPredictions([]);
  }

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
      modelArtifact: null,
    };
    for (const imageElement of [view.sourceImage, view.modelImage]) {
      imageElement.width = detail.media.width;
      imageElement.height = detail.media.height;
    }
    view.scrubber.max = String(frames.frames.length - 1);
    view.title.textContent = detail.title;
    view.openScenario.href = `/scenarios/?scenario=${encodeURIComponent(id)}`;
    await showPlaybackFrame(0, generation);
    if (view.overlayAvailability?.state !== "complete") {
      view.modelState.hidden = false;
      view.modelState.textContent = "Model playback unavailable — this run predates overlay retention.";
      return;
    }
    try {
      const template = view.overlayAvailability.scenario_url_template;
      if (typeof template !== "string" || !template.includes("{scenario_id}")) {
        throw new Error("The model playback location is invalid.");
      }
      const overlayUrl = new URL(template.replace("{scenario_id}", encodeURIComponent(id)), location.origin);
      const expectedPrefix = `/api/v1/submissions/${encodeURIComponent(view.submissionId)}/prediction-overlays/`;
      if (overlayUrl.origin !== location.origin || !overlayUrl.pathname.startsWith(expectedPrefix)) {
        throw new Error("The model playback location is invalid.");
      }
      const modelArtifact = await fetchJson(overlayUrl);
      if (!playback || generation !== playbackGeneration) return;
      playback.modelArtifact = modelArtifact;
      if (modelArtifact.state === "unavailable") {
        view.modelState.hidden = false;
        view.modelState.textContent = "Model playback unavailable — the run exceeded the safe visualization budget.";
      } else {
        await showPlaybackFrame(playback.index, generation);
      }
    } catch {
      if (!playback || generation !== playbackGeneration) return;
      view.modelState.hidden = false;
      view.modelState.textContent = "Model playback unavailable — the retained overlay could not be loaded.";
    }
  } catch (error) {
    if (generation !== playbackGeneration) return;
    view.mediaState.textContent = error.message;
    view.mediaState.hidden = false;
  }
}

function renderPlayback(body) {
  const theater = element("section", undefined, "result-theater");
  const heading = element("div", undefined, "result-theater-heading");
  const headingCopy = element("div");
  headingCopy.append(
    element("p", "Benchmark playback", "kicker"),
    element("h3", "Compare source truth with the model run."),
  );
  const disclosure = element(
    "p",
    "Both panes use the same exact frame and shared controls. The left shows exhaustive source tags; the right shows the submitted system’s retained track projection.",
    "playback-disclosure",
  );
  heading.append(headingCopy, disclosure);

  const reel = element("div", undefined, "result-reel");
  const scenarios = (body.benchmark?.scenario_ids || []).map((id) => ({ id, label: scenarioLabel(id) }));
  const scenarioButtons = scenarios.map((scenario) => {
    const button = element("button", scenario.label);
    button.type = "button";
    button.dataset.scenario = scenario.id;
    button.setAttribute("aria-pressed", "false");
    reel.append(button);
    return button;
  });

  const viewer = element("div", undefined, "result-video");
  viewer.dataset.testid = "comparison-viewer";
  const comparison = element("div", undefined, "result-comparison");
  const sourcePane = element("section", undefined, "result-video-pane");
  sourcePane.dataset.testid = "source-pane";
  sourcePane.append(element("h4", "Source · Ground truth"));
  const sourceStage = element("div", undefined, "result-video-stage");
  const sourceImage = document.createElement("img");
  sourceImage.alt = "";
  sourceImage.dataset.testid = "source-frame";
  const sourceOverlay = svgElement("svg", { "aria-hidden": "true", "data-testid": "source-overlay" });
  const mediaState = element("p", "Loading exact benchmark frames…", "result-media-state");
  sourceStage.append(sourceImage, sourceOverlay, mediaState);
  sourcePane.append(sourceStage, element("p", "Solid green · public ground truth", "ground-truth-legend"));

  const modelPane = element("section", undefined, "result-video-pane");
  modelPane.dataset.testid = "model-pane";
  modelPane.append(element("h4", "Model run · Submitted tracks"));
  const modelStage = element("div", undefined, "result-video-stage");
  const modelImage = document.createElement("img");
  modelImage.alt = "";
  modelImage.dataset.testid = "model-frame";
  const modelOverlay = svgElement("svg", { "aria-hidden": "true", "data-testid": "model-overlay" });
  const modelState = element("p", "Loading submitted tracks…", "result-media-state");
  modelState.dataset.testid = "model-artifact-state";
  modelStage.append(modelImage, modelOverlay, modelState);
  modelPane.append(modelStage, element("p", "Dashed blue · submitted model tracks", "model-legend"));
  comparison.append(sourcePane, modelPane);

  const controls = element("div", undefined, "result-video-controls");
  const previous = element("button", "←");
  previous.type = "button";
  previous.setAttribute("aria-label", "Previous benchmark frame");
  const playButton = element("button", "Play");
  playButton.type = "button";
  playButton.dataset.testid = "comparison-play";
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
  scrubber.dataset.testid = "comparison-scrubber";
  const position = element("output", "0 / 0");
  const time = element("output", "0.000 s");
  const speedLabel = element("label", "Speed ");
  const speed = document.createElement("select");
  speed.setAttribute("aria-label", "Playback speed");
  speed.dataset.testid = "comparison-speed";
  for (const value of [0.5, 1, 2]) {
    const option = element("option", `${value}×`);
    option.value = String(value);
    if (value === 1) option.selected = true;
    speed.append(option);
  }
  speedLabel.append(speed);
  controls.append(previous, playButton, next, scrubber, position, time, speedLabel);

  const footer = element("div", undefined, "result-video-footer");
  const legend = element("p", "Frame-locked comparison", "ground-truth-legend");
  const openScenario = element("a", "Open full frame inspector →");
  footer.append(legend, openScenario);
  viewer.append(comparison, controls, footer);
  theater.append(heading, reel, viewer);

  const view = {
    title: headingCopy.querySelector("h3"),
    scenarioButtons,
    sourceImage,
    sourceOverlay,
    modelImage,
    modelOverlay,
    modelState,
    mediaState,
    previous,
    playButton,
    next,
    scrubber,
    position,
    time,
    speed,
    openScenario,
    submissionId: body.id,
    overlayAvailability: body.result?.prediction_overlay,
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
  if (scenarios.length) loadPlaybackScenario(scenarios[0].id, view);
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
    if (finding.possible_causes?.length) {
      const causes = element("ul", undefined, "finding-causes");
      for (const cause of finding.possible_causes) causes.append(element("li", cause));
      card.append(causes);
    }
    if (finding.recommended_test) {
      const next = element("p", undefined, "finding-next-test");
      next.append(element("strong", "Next test: "), finding.recommended_test);
      card.append(next);
    }
    list.append(card);
  }
  section.append(list);
  return section;
}

function renderAgentFeedback(feedback) {
  const section = element("section", undefined, "agent-feedback");
  const copy = element("div");
  const heading = {
    iterate: "Use this run to choose the next change.",
    fix_and_retry: "Fix the execution boundary, then retry.",
    ready_for_comparison: "Ready for direct comparison.",
  }[feedback?.verdict] || "Agent feedback";
  copy.append(
    element("p", "Agent iteration brief", "kicker"),
    element("h3", heading),
    element("p", feedback?.summary || "Structured agent feedback is unavailable for this historical result."),
  );
  const priorities = element("div", undefined, "iteration-priorities");
  if (feedback?.priorities?.length) {
    for (const priority of feedback.priorities) {
      const card = element("article");
      card.append(
        element("p", `${priority.severity || "unrated"} · ${priority.finding_id || "benchmark finding"}`, "iteration-priority-meta"),
        element("h4", priority.problem || "Review this benchmark finding."),
      );
      if (priority.possible_causes?.length) {
        const causes = element("ul");
        for (const cause of priority.possible_causes) causes.append(element("li", cause));
        card.append(causes);
      }
      if (priority.next_test) {
        const next = element("p", undefined, "iteration-next-test");
        next.append(element("strong", "Run next: "), priority.next_test);
        card.append(next);
      }
      priorities.append(card);
    }
  } else {
    priorities.append(element("p", "No prioritized defects were generated for this run."));
  }
  section.append(copy, priorities);
  return section;
}

function resultShareUrl(id) {
  const url = new URL("/results/", location.origin);
  url.searchParams.set("submission", id);
  return url.toString();
}

function renderBenchmarkContract(benchmark) {
  const section = element("section", undefined, "result-benchmark-contract");
  section.append(
    element("p", "Tested benchmark", "kicker"),
    element("h3", `${benchmark?.id || "Unknown benchmark"} · Version ${benchmark?.version || "unknown"}`),
  );
  const axes = element("dl", undefined, "result-axes");
  const memory = benchmark?.resources?.memory_limit_mb;
  const values = [
    ["Scenarios", benchmark?.scenario_count == null ? "Unavailable" : String(benchmark.scenario_count)],
    ["CPU limit", benchmark?.resources?.cpu_limit == null ? "Unavailable" : `${benchmark.resources.cpu_limit} CPUs`],
    ["Memory limit", memory == null ? "Unavailable" : `${formatNumber(memory / 1024, 0)} GiB`],
    ["Maximum run", benchmark?.run_budgets?.max_run_seconds == null
      ? "Unavailable"
      : `${formatNumber(benchmark.run_budgets.max_run_seconds, 0)} seconds`],
  ];
  for (const [label, value] of values) axes.append(element("dt", label), element("dd", value));
  section.append(
    axes,
    element(
      "p",
      "Scores are directly comparable only when both the comparison fingerprint and leaderboard class match.",
      "benchmark-comparison-note",
    ),
  );
  return section;
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
  output.append(header, renderBenchmarkContract(body.benchmark), timeline, renderLiveProgress(body));

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
          ? "Eligible result with complete trusted-runner accounting. Equal-category comparison still requires the same benchmark fingerprint."
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

    output.append(
      eligibility,
      renderAgentFeedback(body.result.agent_feedback),
      renderPlayback(body),
      groups,
      renderFindings(body.result.findings || []),
    );
  } else {
    const pending = element("section", undefined, "result-pending");
    pending.append(
      element("p", body.status === "failed" ? "Run failed" : "Live queue", "kicker"),
      element("h3", body.status === "failed" ? (body.error || "The runner did not complete this submission.") : "Waiting for a terminal result…"),
      element("p", terminalStatuses.has(body.status)
        ? "The public record is terminal."
        : "This page refreshes automatically while the job is queued or running."),
    );
    if (body.agent_feedback) output.append(renderAgentFeedback(body.agent_feedback));
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
