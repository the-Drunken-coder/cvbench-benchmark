import { access, readFile, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = path.resolve(CONTROL_PLANE, "..");
const OUTPUT = path.join(ROOT, "docs/scenario-catalog-screenshots");
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

const server = http.createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    let filename = path.resolve(CONTROL_PLANE, `.${pathname.startsWith("/") ? `/dist${pathname}` : pathname}`);
    if (!filename.startsWith(`${path.join(CONTROL_PLANE, "dist")}${path.sep}`)) throw new Error("path escape");
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
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ executablePath: await chromeExecutable(), headless: true });

async function capture(filename, scenario, viewport, frame = 0) {
  const page = await browser.newPage({ viewport });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${origin}/scenarios/?scenario=${scenario}`);
  await page.locator("#scenario-detail:not([hidden])").waitFor();
  if (frame) {
    await page.locator("#frame-scrubber").evaluate((scrubber, index) => {
      scrubber.value = String(index);
      scrubber.dispatchEvent(new Event("input", { bubbles: true }));
    }, frame);
  }
  await page.waitForFunction(() => {
    const canvas = document.querySelector("#scenario-frame");
    return canvas?.dataset.frameReady === "true" && document.querySelector("#media-state")?.hidden;
  });
  await page.locator("#scenario-detail").screenshot({ path: path.join(OUTPUT, filename) });
  await page.close();
}

async function captureMissingMedia(filename, scenario, viewport) {
  const page = await browser.newPage({ viewport });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/*.jpg", (route) => route.fulfill({ status: 404, contentType: "text/plain", body: "Not found" }));
  await page.goto(`${origin}/scenarios/?scenario=${scenario}`);
  await page.locator("#scenario-detail:not([hidden])").waitFor();
  await page.locator("#media-state:not([hidden])").waitFor();
  await page.locator("#scenario-detail").screenshot({ path: path.join(OUTPUT, filename) });
  await page.close();
}

try {
  await capture("desktop-synthetic.png", "synthetic-acquisition", { width: 1440, height: 1100 }, 2);
  await capture("mobile-synthetic-390px.png", "synthetic-acquisition", { width: 390, height: 900 }, 2);
  await capture("real-full-frame-mot.png", "rvmot-a1c9", { width: 1440, height: 1100 }, 75);
  await capture("mobile-real-390px.png", "rvmot-c4f6", { width: 390, height: 900 }, 100);
  await capture("empty-ground-truth.png", "synthetic-false-detection", { width: 1280, height: 900 });
  await capture("long-sequence.png", "synthetic-resource-stress", { width: 1280, height: 900 }, 120);
  await captureMissingMedia("missing-media-state.png", "rvmot-a1c9", { width: 1280, height: 900 });
  await capture("navigation-race-final.png", "rvmot-c4f6", { width: 1280, height: 900 }, 100);
} finally {
  await browser.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
