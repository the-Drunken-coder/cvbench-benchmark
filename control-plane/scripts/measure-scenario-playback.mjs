import { access, readFile, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const controlPlaneArgument = process.argv.find((value) => value.startsWith("--control-plane="))?.split("=")[1];
const CONTROL_PLANE = path.resolve(controlPlaneArgument || path.dirname(fileURLToPath(import.meta.url)), controlPlaneArgument ? "." : "..");
const delayMilliseconds = Number(process.argv.find((value) => value.startsWith("--media-delay="))?.split("=")[1] || 20);
const MIME_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
]);

async function chromeExecutable() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next conventional Chrome location.
    }
  }
  throw new Error("Chrome is required. Set CHROME_BIN if it is installed elsewhere.");
}

const server = http.createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    let filename = path.resolve(CONTROL_PLANE, `.${pathname.startsWith("/") ? `/dist${pathname}` : pathname}`);
    if (!filename.startsWith(`${path.join(CONTROL_PLANE, "dist")}${path.sep}`)) throw new Error("path escape");
    if ((await stat(filename)).isDirectory()) filename = path.join(filename, "index.html");
    if (path.extname(filename) === ".jpg" && delayMilliseconds > 0) {
      await new Promise((resolve) => setTimeout(resolve, delayMilliseconds));
    }
    const body = await readFile(filename);
    response.writeHead(200, { "content-type": MIME_TYPES.get(path.extname(filename)) || "application/octet-stream" });
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
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const origin = `http://127.0.0.1:${server.address().port}`;
  await page.goto(`${origin}/scenarios/?scenario=rvmot-a1c9`);
  await page.locator("#scenario-detail:not([hidden])").waitFor();
  await page.waitForFunction(() => {
    const media = document.querySelector("#scenario-frame");
    return media instanceof HTMLCanvasElement
      ? media.dataset.frameReady === "true"
      : media?.complete && media.naturalWidth > 0 && document.querySelector("#media-state")?.hidden;
  });

  const result = await page.evaluate(async () => {
    const media = document.querySelector("#scenario-frame");
    const scrubber = document.querySelector("#frame-scrubber");
    const samples = [];
    let blankAnimationFrames = 0;
    let running = true;
    const sample = () => {
      const blank = media instanceof HTMLCanvasElement
        ? media.dataset.frameReady !== "true"
        : !media.getAttribute("src") || !media.complete || media.naturalWidth === 0;
      if (blank) blankAnimationFrames += 1;
      samples.push({ frame: Number(scrubber.value), time: performance.now() });
      if (running) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
    const startedAt = performance.now();
    document.querySelector("#play-pause").click();
    await new Promise((resolve) => setTimeout(resolve, 1200));
    running = false;
    document.querySelector("#play-pause").click();
    await new Promise(requestAnimationFrame);
    const endedAt = performance.now();
    const firstFrame = samples[0]?.frame ?? 0;
    const lastFrame = Number(scrubber.value);
    const sourceElapsedMs = (lastFrame - firstFrame) * (1000 / 30);
    const wallElapsedMs = endedAt - startedAt;
    return {
      blankAnimationFrames,
      firstFrame,
      lastFrame,
      mediaDelayMs: Number(new URL(location.href).searchParams.get("mediaDelay")) || null,
      presentedFrames: new Set(samples.map(({ frame }) => frame)).size,
      sourceElapsedMs: Number(sourceElapsedMs.toFixed(1)),
      wallElapsedMs: Number(wallElapsedMs.toFixed(1)),
      driftMs: Number((sourceElapsedMs - wallElapsedMs).toFixed(1)),
    };
  });
  result.mediaDelayMs = delayMilliseconds;
  console.log(JSON.stringify(result));
} finally {
  await browser.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
