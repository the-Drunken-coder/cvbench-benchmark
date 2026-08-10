import assert from "node:assert/strict";
import { access, readFile, rm, stat } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MIME_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".mp4", "video/mp4"],
]);

async function chromeExecutable() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next standard Chrome location.
    }
  }
  throw new Error("Chrome is required for training-media browser tests. Set CHROME_BIN if needed.");
}

async function staticServer(root) {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      let filename = path.resolve(root, `.${pathname}`);
      if (!filename.startsWith(`${root}${path.sep}`)) throw new Error("Path escapes fixture root.");
      if ((await stat(filename)).isDirectory()) filename = path.join(filename, "index.html");
      const body = await readFile(filename);
      response.writeHead(200, {
        "content-length": body.length,
        "content-type": MIME_TYPES.get(path.extname(filename)) || "application/octet-stream",
      });
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
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

function build(output) {
  const result = spawnSync(process.execPath, ["scripts/build-scenario-catalog.mjs", "--output", output], {
    cwd: CONTROL_PLANE,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
}

async function waitForTrainingPage(page, origin) {
  await page.goto(`${origin}/training/`);
  await page.getByText("5 transformed training previews loaded. None are evaluation-eligible.").waitFor();
  assert.equal(await page.locator(".training-video-card").count(), 5);
  assert.equal(await page.locator("video").count(), 5);
  assert.equal(await page.getByText("0 evaluation scenarios").count(), 1);
}

test("training previews render and play on desktop and mobile", async (context) => {
  const outputName = "dist-test-training-browser";
  const output = path.join(CONTROL_PLANE, outputName);
  context.after(async () => rm(output, { recursive: true, force: true }));
  build(outputName);
  const server = await staticServer(output);
  context.after(server.close);
  const browser = await chromium.launch({
    executablePath: await chromeExecutable(),
    headless: true,
    args: ["--autoplay-policy=no-user-gesture-required"],
  });
  context.after(() => browser.close());

  const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await waitForTrainingPage(desktop, server.origin);
  const firstVideo = desktop.locator("video").first();
  await firstVideo.evaluate(async (video) => {
    video.muted = true;
    await video.play();
  });
  await desktop.waitForFunction(() => document.querySelector("video")?.currentTime > 0.4);
  assert.ok(await firstVideo.evaluate((video) => video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA));

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await waitForTrainingPage(mobile, server.origin);
  assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);

  if (process.env.CVBENCH_CAPTURE_TRAINING_SCREENSHOTS) {
    await desktop.screenshot({ path: path.join(os.tmpdir(), "cvbench-training-desktop.png"), fullPage: true });
    await mobile.screenshot({ path: path.join(os.tmpdir(), "cvbench-training-mobile.png"), fullPage: true });
  }
});
