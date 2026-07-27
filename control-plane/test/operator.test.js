import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const operatorSource = readFileSync(new URL("../public/operator.js", import.meta.url), "utf8");

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.handlers = new Map();
    this.value = "";
    this.hidden = false;
    this.checked = true;
    this._text = "";
  }

  addEventListener(type, handler) {
    this.handlers.set(type, handler);
  }

  async dispatch(type, event = {}) {
    return this.handlers.get(type)?.({ preventDefault() {}, ...event });
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  set textContent(value) {
    this._text = String(value);
  }

  get textContent() {
    return this._text;
  }
}

function makeHarness() {
  const ids = [
    "operator-auth",
    "operator-token",
    "adjudicator-token",
    "operator-console",
    "refresh",
    "load-more",
    "status-filter",
    "auto-refresh",
    "job-list",
    "job-detail",
    "adjudication-form",
    "adjudication-verdict",
    "adjudication-note",
    "adjudication-status",
  ];
  const elements = new Map(ids.map((id) => [id, new FakeElement()]));
  elements.get("status-filter").value = "";
  elements.get("adjudication-verdict").value = "accepted";
  const document = {
    querySelector(selector) {
      return elements.get(selector.slice(1));
    },
    createElement(tag) {
      return new FakeElement(tag);
    },
  };
  const timers = [];
  const context = {
    document,
    URLSearchParams,
    JSON,
    encodeURIComponent,
    console,
    setTimeout(callback) {
      timers.push(callback);
      return timers.length;
    },
    clearTimeout() {},
    AbortController: class {
      constructor() {
        this.signal = {};
      }

      abort() {
        this.signal.aborted = true;
      }
    },
  };
  context.globalThis = context;
  let hooks;
  context.__CVBENCH_OPERATOR_TEST_HOOK__ = (value) => { hooks = value; };
  vm.runInNewContext(operatorSource, context, { filename: "operator.js" });
  return { context, elements, hooks, timers };
}

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
    async text() {
      return JSON.stringify(body);
    },
  };
}

function jobs(name) {
  return {
    next_cursor: null,
    jobs: [{
      id: `${name}-job`,
      status: "running",
      model: { name, version: "1" },
      queue: { attempt: 1 },
    }],
  };
}

function installRequestQueue(harness) {
  const requests = [];
  harness.context.fetch = (url, init) => new Promise((resolve, reject) => {
    requests.push({ url, init, resolve, reject, claimed: false });
  });
  return requests;
}

function takeQueuedRequest(requests, predicate) {
  const request = requests.find((candidate) => !candidate.claimed && predicate(candidate));
  assert.ok(request, "expected queued request was not found");
  request.claimed = true;
  return request;
}

async function waitForQueuedRequest(requests, predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const request = requests.find((candidate) => !candidate.claimed && predicate(candidate));
    if (request) {
      request.claimed = true;
      return request;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail("expected queued request was not created");
}

function isListRequest(request) {
  return request.url.startsWith("/api/v1/operator/jobs?");
}

function isDetailRequest(request, id) {
  return request.url === `/api/v1/operator/jobs/${encodeURIComponent(id)}`;
}

test("operator console discards stale list and detail responses", async () => {
  const harness = makeHarness();
  harness.hooks.setTokens("read-token", "write-token");
  const requests = installRequestQueue(harness);

  const staleList = harness.hooks.loadJobs({ reset: true });
  const currentList = harness.hooks.loadJobs({ reset: true });
  const staleListRequest = takeQueuedRequest(requests, isListRequest);
  const currentListRequest = takeQueuedRequest(requests, isListRequest);
  currentListRequest.resolve(response(jobs("new")));
  await currentList;
  staleListRequest.resolve(response(jobs("old")));
  await staleList;
  assert.match(harness.elements.get("job-list").children[0].children[0].textContent, /^new/);

  const staleDetail = harness.hooks.selectJob("old-job");
  const currentDetail = harness.hooks.selectJob("new-job");
  const staleDetailRequest = takeQueuedRequest(requests, (request) => isDetailRequest(request, "old-job"));
  const currentDetailRequest = takeQueuedRequest(requests, (request) => isDetailRequest(request, "new-job"));
  currentDetailRequest.resolve(response({ job: { model: { name: "new" }, status: "succeeded" } }));
  await currentDetail;
  staleDetailRequest.resolve(response({ job: { model: { name: "old" }, status: "failed" } }));
  await staleDetail;
  assert.match(harness.elements.get("job-detail").children[0].textContent, /^new/);
});

test("detail selection does not invalidate a current list response", async () => {
  const harness = makeHarness();
  harness.hooks.setTokens("read-token", "write-token");
  const requests = installRequestQueue(harness);

  const listRequest = harness.hooks.loadJobs({ reset: true });
  const detailRequest = harness.hooks.selectJob("selected-job");
  const listFetch = takeQueuedRequest(requests, isListRequest);
  const detailFetch = takeQueuedRequest(requests, (request) => isDetailRequest(request, "selected-job"));
  detailFetch.resolve(response({ job: { model: { name: "selected" }, status: "running" } }));
  await detailRequest;
  listFetch.resolve(response({
    next_cursor: "cursor-after-list",
    jobs: [{ id: "listed-job", status: "queued", model: { name: "listed", version: "1" }, queue: { attempt: 2 } }],
  }));
  const refreshedDetail = await waitForQueuedRequest(requests, (request) => isDetailRequest(request, "selected-job"));
  refreshedDetail.resolve(response({ job: { model: { name: "selected" }, status: "running" } }));
  await listRequest;

  assert.equal(harness.elements.get("job-list").children.length, 1);
  assert.equal(harness.elements.get("load-more").hidden, false);
});

test("operator polling reschedules after transport and JSON failures", async () => {
  const harness = makeHarness();
  harness.hooks.setTokens("read-token", "write-token");
  harness.context.fetch = async () => { throw new Error("offline"); };
  harness.hooks.scheduleRefresh();
  const transportTimer = harness.timers.shift();
  await transportTimer();
  assert.equal(harness.timers.length, 1);
  assert.match(harness.elements.get("job-detail").textContent, /offline/);

  harness.context.fetch = async () => ({ ok: true, status: 200, async json() { throw new Error("invalid JSON"); } });
  const jsonTimer = harness.timers.shift();
  await jsonTimer();
  assert.equal(harness.timers.length, 1);
  assert.match(harness.elements.get("job-detail").textContent, /invalid JSON/);
});

test("operator note form uses the separate adjudicator credential", async () => {
  const harness = makeHarness();
  harness.hooks.setTokens("read-token", "write-token");
  const requests = [];
  harness.context.fetch = async (url, init) => {
    requests.push({ url, init });
    if (init?.method === "POST") return response({ actorId: "operator/alice" }, 201);
    return response({ job: { model: { name: "tracker" }, status: "running" } });
  };
  await harness.hooks.selectJob("job-1");
  harness.elements.get("adjudication-note").value = "Reviewed evidence.";
  await harness.elements.get("adjudication-form").dispatch("submit");
  const noteRequest = requests.find(({ init }) => init?.method === "POST");
  assert.equal(noteRequest.init.headers.authorization, "Bearer write-token");
  assert.notEqual(noteRequest.init.headers.authorization, "Bearer read-token");
  assert.deepEqual(JSON.parse(noteRequest.init.body), { verdict: "accepted", note: "Reviewed evidence." });
  assert.match(harness.elements.get("adjudication-status").textContent, /operator\/alice/);
});
