import assert from "node:assert/strict";
import { access, readFile, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DIST = path.join(CONTROL_PLANE, "dist");
const TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
]);

async function chromeExecutable() {
  for (const candidate of [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean)) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next conventional installation.
    }
  }
  throw new Error("Chrome is required; set CHROME_BIN if it is not in a conventional location.");
}

function publicRecord() {
  return {
    id: "e27063e8-4436-46e6-bdb5-54941cfd499d",
    status: "succeeded",
    model: {
      name: "CVBench Synthetic Color Tracker",
      version: "759b01d-v1",
      image: `ghcr.io/example/tracker@sha256:${"a".repeat(64)}`,
      argv: ["python", "-m", "cvbench.examples.good_tracker"],
    },
    benchmark: {
      id: "public-whole-system-tracking",
      version: "2.0.0",
      resources: { cpu_limit: 4, memory_limit_mb: 2048, network_access: false },
      run_budgets: { max_run_seconds: 90 },
      scenario_count: 3,
      scenario_ids: ["rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6"],
    },
    attempt: 2,
    result: {
      scores: {
        sample_counts: { output_records: 18906 },
        acquisition_rate: 0.843373,
        observed_coverage: 0.344359,
        continuity_coverage: 0.34554,
        mean_iou: 0.631414,
        id_switches: 0,
        false_track_births: 704,
        reacquisition_same_id_rate: 1,
        latency_p50_ms: 8.08,
        latency_p99_ms: 10.37,
        processing_latency_p95_ms: 9.64,
        real_time_factor: 1.01,
        cpu_seconds_per_native_source_second: 0.183,
        average_cpu_percent: 17.9,
        peak_ram_bytes: 26107904,
        wall_seconds: 39.66,
        replay_profile: "native",
        replay_rate: 1,
        leaderboard_class: "native/cpu-1/realtime",
        leaderboard_eligible: true,
        accounting_complete: true,
      },
      findings: [{
        finding_id: "TRACK-QUALITY-001",
        category: "tracking_accuracy",
        severity: "high",
        statement: "The system missed a substantial portion of eligible target observations.",
      }],
      prediction_overlay: {
        state: "complete",
        schema_version: "cvbench.prediction-overlay/v1",
        scenario_url_template: `/api/v1/submissions/e27063e8-4436-46e6-bdb5-54941cfd499d/prediction-overlays/{scenario_id}`,
        root_sha256: "a".repeat(64),
      },
    },
    error: null,
    created_at: "2026-07-25T23:20:36.000Z",
    started_at: "2026-07-25T23:21:46.000Z",
    completed_at: "2026-07-25T23:29:32.000Z",
  };
}

test("quick submit safely retries and opens the formatted playback result", async () => {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      let filename = path.resolve(DIST, `.${pathname === "/" ? "/index.html" : pathname}`);
      if (!filename.startsWith(`${DIST}${path.sep}`)) throw new Error("path escape");
      if ((await stat(filename)).isDirectory()) filename = path.join(filename, "index.html");
      const body = await readFile(filename);
      response.writeHead(200, { "content-type": TYPES.get(path.extname(filename)) || "application/octet-stream" });
      response.end(body);
    } catch {
      response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      response.end("Not found");
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  const browser = await chromium.launch({ executablePath: await chromeExecutable(), headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const consoleErrors = [];
  const idempotencyKeys = [];
  let postedBody = null;
  let postAttempts = 0;
  let currentRecord = publicRecord();
  let artifactUnavailable = false;
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.route("**/api/v1/submissions**", async (route) => {
    const request = route.request();
    if (request.url().includes("/prediction-overlays/")) {
      const scenarioId = request.url().split("/").at(-1);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(artifactUnavailable ? {
          schema_version: "cvbench.prediction-overlay/v1",
          state: "unavailable",
          reason: "budget_exceeded",
          scenario_id: scenarioId,
          width: 896,
          height: 504,
          frame_count: 150,
          frames: [],
          summary: { prediction_count: 0 },
        } : {
          schema_version: "cvbench.prediction-overlay/v1",
          state: "complete",
          scenario_id: scenarioId,
          width: 896,
          height: 504,
          frame_count: 150,
          frames: Array.from({ length: 150 }, (_, frame_index) => ({
            frame_index,
            source_timestamp_ns: Math.round(frame_index * 1_000_000_000 / 30),
            objects: frame_index === 0 ? [{
              track_label: "track-001",
              class_id: "vehicle",
              event: "track_update",
              state: "confirmed",
              support: "observed",
              confidence: 0.9,
              bbox_xyxy: [10, 20, 100, 120],
            }] : [],
          })),
          summary: { prediction_count: 1 },
        }),
      });
      return;
    }
    if (request.method() === "POST") {
      postAttempts += 1;
      idempotencyKeys.push(request.headers()["idempotency-key"]);
      postedBody = request.postDataJSON();
      if (postAttempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ error: { message: "Temporary queue interruption." } }),
        });
        return;
      }
    }
    await route.fulfill({ status: request.method() === "POST" ? 201 : 200, contentType: "application/json", body: JSON.stringify(currentRecord) });
  });

  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.locator("[name=image]").fill(`ghcr.io/example/tracker@sha256:${"a".repeat(64)}`);
    await page.locator("[name=name]").fill("CVBench Synthetic Color Tracker");
    await page.locator("[name=model_version]").fill("759b01d-v1");
    await page.locator(".argv-row input").nth(2).fill("cvbench.examples.good_tracker");
    await page.locator("[name=api_key]").fill("temporary-browser-key");
    await page.getByRole("button", { name: "Queue system" }).click();
    await assert.doesNotReject(page.getByText("Temporary queue interruption.").waitFor());
    await page.getByRole("button", { name: "Queue system" }).click();
    await page.waitForURL(/\/results\/\?submission=e27063e8-4436-46e6-bdb5-54941cfd499d$/);

    assert.equal(idempotencyKeys.length, 2);
    assert.equal(idempotencyKeys[0], idempotencyKeys[1], "retry must reuse the same idempotency key");
    assert.match(idempotencyKeys[0], /^web-[0-9a-f-]{36}$/);
    assert.deepEqual(postedBody.argv, ["python", "-m", "cvbench.examples.good_tracker"]);
    assert.equal("api_key" in postedBody, false);
    assert.match(page.url(), /\/results\/\?submission=/);

    await page.locator('[data-testid="source-frame"]').evaluate(
      (image) => image.complete && image.naturalWidth > 0 || new Promise((resolve) => image.addEventListener("load", resolve, { once: true })),
    );
    await page.locator(".result-ground-truth-box").first().waitFor();
    await page.locator(".result-model-box").first().waitFor();
    assert.equal(await page.locator(".result-video-stage img").count(), 2);
    assert.equal(await page.getByText("84.3%").first().textContent(), "84.3%");
    assert.equal(await page.getByText("24.9 MiB").textContent(), "24.9 MiB");
    await page.getByRole("heading", { name: "public-whole-system-tracking · Version 2.0.0" }).waitFor();
    assert.equal(await page.getByText("2 GiB").textContent(), "2 GiB");
    assert.equal(await page.getByText("90 seconds").textContent(), "90 seconds");
    assert.equal(await page.locator(".raw-result").getAttribute("open"), null);
    assert.match(await page.locator(".playback-disclosure").textContent(), /submitted system’s retained track projection/);

    const initialPosition = await page.locator(".result-video-controls output").first().textContent();
    await page.getByRole("button", { name: "Play benchmark footage" }).click();
    await page.waitForFunction((before) => document.querySelector(".result-video-controls output")?.textContent !== before, initialPosition);
    await page.getByRole("button", { name: "Pause benchmark footage" }).click();
    assert.equal(
      await page.locator('[data-testid="source-frame"]').getAttribute("src"),
      await page.locator('[data-testid="model-frame"]').getAttribute("src"),
    );
    await page.locator('[data-testid="comparison-scrubber"]').evaluate((input) => {
      input.value = "1";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.getByText("No model tracks emitted for this frame.").waitFor();
    await page.getByRole("button", { name: "Close handoff" }).click();
    await page.getByRole("heading", { name: "Close-proximity handoff" }).waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
      true,
    );
    const sourcePane = await page.locator('[data-testid="source-pane"]').boundingBox();
    const modelPane = await page.locator('[data-testid="model-pane"]').boundingBox();
    assert.ok(modelPane.y >= sourcePane.y + sourcePane.height, "mobile comparison panes must stack");

    artifactUnavailable = true;
    await page.goto(`http://127.0.0.1:${server.address().port}/results/?submission=${currentRecord.id}`);
    await page.getByText("Model playback unavailable — the run exceeded the safe visualization budget.").waitFor();

    artifactUnavailable = false;
    currentRecord = publicRecord();
    currentRecord.result.prediction_overlay = { state: "unavailable", reason: "legacy_run" };
    await page.goto(`http://127.0.0.1:${server.address().port}/results/?submission=${currentRecord.id}`);
    await page.getByText("Model playback unavailable — this run predates overlay retention.").waitFor();

    currentRecord = publicRecord();
    await page.goto(`http://127.0.0.1:${server.address().port}/#results?submission=${publicRecord().id}`);
    await page.waitForURL(new RegExp(`/results/\\?submission=${publicRecord().id}$`));
    await page.getByRole("heading", { name: "CVBench Synthetic Color Tracker" }).waitFor();
    await page.getByLabel("Submission UUID").fill(publicRecord().id);
    await page.getByRole("button", { name: "Open result" }).click();
    await page.getByRole("heading", { name: "CVBench Synthetic Color Tracker" }).waitFor();

    assert.deepEqual(
      consoleErrors.filter((message) => !/status of 503/.test(message)),
      [],
    );
  } finally {
    await browser.close();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
