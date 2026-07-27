import assert from "node:assert/strict";
import { test } from "node:test";

import { createLatestScenarioLoader, exactFrameFailureMessage, renderExactFrameFailure } from "../public/scenario-loader.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("a delayed detail request cannot overwrite the current history selection", async () => {
  const loader = createLatestScenarioLoader();
  const first = deferred();
  const second = deferred();
  let url = "/scenarios/?scenario=synthetic-acquisition";
  let view = null;
  const selectedId = () => new URL(url, "https://cvbench.example").searchParams.get("scenario");
  const commit = async (bundle) => { view = bundle; };

  const firstLoad = loader.load("synthetic-acquisition", () => first.promise, selectedId, commit);
  url = "/scenarios/?scenario=rvmot-b7e2";
  const secondLoad = loader.load("rvmot-b7e2", () => second.promise, selectedId, commit);
  second.resolve({ detail: { id: "rvmot-b7e2" }, frames: { scenario_id: "rvmot-b7e2" }, annotations: { scenario_id: "rvmot-b7e2" } });
  assert.equal(await secondLoad, true);
  first.resolve({ detail: { id: "synthetic-acquisition" }, frames: { scenario_id: "synthetic-acquisition" }, annotations: { scenario_id: "synthetic-acquisition" } });
  assert.equal(await firstLoad, false);

  assert.equal(selectedId(), "rvmot-b7e2");
  assert.equal(view.detail.id, "rvmot-b7e2");
  assert.equal(view.frames.scenario_id, "rvmot-b7e2");
  assert.equal(view.annotations.scenario_id, "rvmot-b7e2");
});

test("frame retrieval status distinguishes missing from unavailable", () => {
  assert.equal(exactFrameFailureMessage(404), "Exact frame is missing (404).");
  assert.equal(exactFrameFailureMessage(500), "Exact frame is unavailable because retrieval failed (500).");
  assert.equal(exactFrameFailureMessage(503), "Exact frame is unavailable because retrieval failed (503).");
});

test("the media status UI presents retrieval failures without claiming absence", () => {
  const classes = new Set();
  const output = {
    hidden: true,
    textContent: "Verifying exact frame hash…",
    classList: { add: (name) => classes.add(name) },
  };
  renderExactFrameFailure(output, new Error(exactFrameFailureMessage(503)));
  assert.equal(output.hidden, false);
  assert.equal(classes.has("error"), true);
  assert.equal(output.textContent, "Exact frame is unavailable because retrieval failed (503).");
});
