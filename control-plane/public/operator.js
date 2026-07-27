let operatorToken = "";
let adjudicatorToken = "";
let selectedJobId = "";
let timer;
let nextCursor = null;
let listRequestGeneration = 0;
let detailRequestGeneration = 0;
let pollGeneration = 0;
let adjudicationGeneration = 0;
let jobsController;
let detailController;

document.querySelector("#operator-auth")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const readInput = document.querySelector("#operator-token");
  const writeInput = document.querySelector("#adjudicator-token");
  operatorToken = readInput.value;
  adjudicatorToken = writeInput.value;
  readInput.value = "";
  writeInput.value = "";
  document.querySelector("#operator-console").hidden = false;
  await refreshJobs();
  scheduleRefresh();
});
document.querySelector("#refresh")?.addEventListener("click", refreshJobs);
document.querySelector("#load-more")?.addEventListener("click", () => loadJobs());
document.querySelector("#status-filter")?.addEventListener("change", refreshJobs);
document.querySelector("#auto-refresh")?.addEventListener("change", scheduleRefresh);
document.querySelector("#adjudication-form")?.addEventListener("submit", submitAdjudication);

async function refreshJobs() {
  await loadJobs({ reset: true });
}

function isCurrent(generation, flow) {
  return generation === (flow === "list" ? listRequestGeneration : detailRequestGeneration);
}

function currentResponseError(response) {
  return `Operator API returned ${response.status}.`;
}

async function loadJobs({ reset = false } = {}) {
  if (!operatorToken) return;
  const generation = ++listRequestGeneration;
  if (jobsController) jobsController.abort();
  const controller = new AbortController();
  jobsController = controller;
  if (reset) nextCursor = null;
  const status = document.querySelector("#status-filter").value;
  const cursor = reset ? null : nextCursor;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (cursor) params.set("cursor", cursor);
  params.set("limit", "25");
  try {
    const response = await fetch(`/api/v1/operator/jobs?${params}`, {
      headers: { authorization: `Bearer ${operatorToken}` },
      signal: controller.signal,
    });
    if (!isCurrent(generation, "list")) return;
    if (!response.ok) return showMessage(currentResponseError(response), () => isCurrent(generation, "list"));
    const body = await response.json();
    if (!isCurrent(generation, "list")) return;
    const list = document.querySelector("#job-list");
    if (reset) list.replaceChildren();
    nextCursor = body.next_cursor;
    for (const job of body.jobs) {
      if (!isCurrent(generation, "list")) return;
      const button = document.createElement("button");
      button.className = `job-row${job.id === selectedJobId ? " selected" : ""}`;
      button.type = "button";
      button.addEventListener("click", () => selectJob(job.id));
      const title = document.createElement("strong");
      title.textContent = `${job.model.name} · ${job.model.version}`;
      const meta = document.createElement("span");
      meta.textContent = `${job.status} · attempt ${job.queue.attempt} · ${job.id}`;
      button.append(title, meta);
      list.append(button);
    }
    if (!isCurrent(generation, "list")) return;
    const loadMore = document.querySelector("#load-more");
    loadMore.hidden = !nextCursor;
    if (selectedJobId) await selectJob(selectedJobId);
  } catch (error) {
    if (isCurrent(generation, "list") && error.name !== "AbortError") {
      showMessage(`Operator API request failed: ${error.message}`, () => isCurrent(generation, "list"));
    }
  } finally {
    if (jobsController === controller) jobsController = undefined;
  }
}

async function selectJob(id) {
  selectedJobId = id;
  const generation = ++detailRequestGeneration;
  if (detailController) detailController.abort();
  const controller = new AbortController();
  detailController = controller;
  try {
    const response = await fetch(`/api/v1/operator/jobs/${encodeURIComponent(id)}`, {
      headers: { authorization: `Bearer ${operatorToken}` },
      signal: controller.signal,
    });
    if (!isCurrent(generation, "detail")) return;
    if (!response.ok) return showMessage(`Could not load job (${response.status}).`, () => isCurrent(generation, "detail"));
    const body = await response.json();
    if (!isCurrent(generation, "detail") || selectedJobId !== id) return;
    const title = document.createElement("h3");
    title.textContent = `${body.job.model.name} · ${body.job.status}`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(body, null, 2);
    document.querySelector("#job-detail").replaceChildren(title, pre);
  } catch (error) {
    if (isCurrent(generation, "detail") && error.name !== "AbortError") {
      showMessage(`Operator API request failed: ${error.message}`, () => isCurrent(generation, "detail"));
    }
  } finally {
    if (detailController === controller) detailController = undefined;
  }
}

function scheduleRefresh() {
  clearTimeout(timer);
  timer = undefined;
  const generation = ++pollGeneration;
  if (document.querySelector("#auto-refresh")?.checked && operatorToken) {
    timer = setTimeout(async () => {
      try {
        await refreshJobs();
      } catch (error) {
        if (generation === pollGeneration) showMessage(`Polling failed: ${error.message}`, () => generation === pollGeneration);
      } finally {
        if (generation === pollGeneration) scheduleRefresh();
      }
    }, 5000);
  }
}

async function submitAdjudication(event) {
  event.preventDefault();
  const status = document.querySelector("#adjudication-status");
  const jobId = selectedJobId;
  const generation = ++adjudicationGeneration;
  if (!jobId) {
    status.textContent = "Select a job before saving an adjudication note.";
    return;
  }
  if (!adjudicatorToken) {
    status.textContent = "An adjudicator write token is required.";
    return;
  }
  try {
    const response = await fetch(`/api/v1/operator/jobs/${encodeURIComponent(jobId)}/notes`, {
      method: "POST",
      headers: { authorization: `Bearer ${adjudicatorToken}`, "content-type": "application/json" },
      body: JSON.stringify({
        verdict: document.querySelector("#adjudication-verdict").value,
        note: document.querySelector("#adjudication-note").value,
      }),
    });
    const text = await response.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
    if (generation !== adjudicationGeneration) return;
    if (!response.ok) {
      status.textContent = body?.error?.message || `Could not save note (${response.status}).`;
      return;
    }
    status.textContent = `Saved adjudication note as ${body.actorId}.`;
    document.querySelector("#adjudication-note").value = "";
  } catch (error) {
    if (generation === adjudicationGeneration) status.textContent = `Could not save note: ${error.message}`;
  }
}

function showMessage(message, isFresh = () => true) {
  if (!isFresh()) return;
  const detail = document.querySelector("#job-detail");
  detail.textContent = message;
}

if (globalThis.__CVBENCH_OPERATOR_TEST_HOOK__) {
  globalThis.__CVBENCH_OPERATOR_TEST_HOOK__({ loadJobs, selectJob, refreshJobs, scheduleRefresh, setTokens: (read, write) => {
    operatorToken = read;
    adjudicatorToken = write;
  } });
}
