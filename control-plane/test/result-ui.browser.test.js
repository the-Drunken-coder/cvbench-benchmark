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
    benchmark: { id: "public-whole-system-tracking", version: "2.0.0" },
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
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await page.route("**/api/v1/submissions**", async (route) => {
    const request = route.request();
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
    await route.fulfill({ status: request.method() === "POST" ? 201 : 200, contentType: "application/json", body: JSON.stringify(publicRecord()) });
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
    await assert.doesNotReject(page.getByText(/Opening its live result studio/).waitFor());

    assert.equal(idempotencyKeys.length, 2);
    assert.equal(idempotencyKeys[0], idempotencyKeys[1], "retry must reuse the same idempotency key");
    assert.match(idempotencyKeys[0], /^web-[0-9a-f-]{36}$/);
    assert.deepEqual(postedBody.argv, ["python", "-m", "cvbench.examples.good_tracker"]);
    assert.equal("api_key" in postedBody, false);
    assert.equal(await page.locator("[name=api_key]").inputValue(), "");

    await page.locator(".result-video-stage img").evaluate(
      (image) => image.complete && image.naturalWidth > 0 || new Promise((resolve) => image.addEventListener("load", resolve, { once: true })),
    );
    await page.locator(".result-ground-truth-box").first().waitFor();
    assert.equal(await page.getByText("84.3%").first().textContent(), "84.3%");
    assert.equal(await page.getByText("24.9 MiB").textContent(), "24.9 MiB");
    assert.equal(await page.locator(".raw-result").getAttribute("open"), null);
    assert.match(await page.locator(".playback-disclosure").textContent(), /not the submitted system’s predictions/);

    const initialPosition = await page.locator(".result-video-controls output").first().textContent();
    await page.getByRole("button", { name: "Play benchmark footage" }).click();
    await page.waitForFunction((before) => document.querySelector(".result-video-controls output")?.textContent !== before, initialPosition);
    await page.getByRole("button", { name: "Pause benchmark footage" }).click();
    await page.getByRole("button", { name: "Close handoff" }).click();
    await page.getByRole("heading", { name: "Close-proximity handoff" }).waitFor();

    assert.deepEqual(
      consoleErrors.filter((message) => !/status of 503/.test(message)),
      [],
    );
  } finally {
    await browser.close();
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
