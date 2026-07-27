import assert from "node:assert/strict";
import { access, readFile, rm, stat } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
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
  throw new Error("Chrome is required for scenario layout regression tests. Set CHROME_BIN if it is installed elsewhere.");
}

async function staticServer(root, { mediaDelay = 0, mediaRequests = [] } = {}) {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      let filename = path.resolve(root, `.${pathname}`);
      if (!filename.startsWith(`${root}${path.sep}`)) throw new Error("Path escapes browser fixture root.");
      if ((await stat(filename)).isDirectory()) filename = path.join(filename, "index.html");
      if (path.extname(filename) === ".jpg") {
        mediaRequests.push(pathname);
        if (mediaDelay) await new Promise((resolve) => setTimeout(resolve, mediaDelay));
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
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

function assertBoundsMatch(actual, expected, label, tolerance = 0.15) {
  for (const property of ["x", "y", "width", "height"]) {
    assert.ok(Math.abs(actual[property] - expected[property]) <= tolerance,
      `${label} ${property}: ${actual[property]} != ${expected[property]}`);
  }
}

async function loadScenario(page, origin, scenarioId, frameIndex = 0) {
  await page.goto(`${origin}/scenarios/?scenario=${scenarioId}`);
  await page.locator("#scenario-detail:not([hidden])").waitFor();
  if (frameIndex !== 0) {
    await page.locator("#frame-scrubber").evaluate((scrubber, index) => {
      scrubber.value = String(index);
      scrubber.dispatchEvent(new Event("input", { bubbles: true }));
    }, frameIndex);
  }
  await page.waitForFunction(() => {
    const canvas = document.querySelector("#scenario-frame");
    return canvas?.dataset.frameReady === "true" && document.querySelector("#media-state")?.hidden;
  });
}

async function frameGeometry(page) {
  return page.evaluate(() => {
    const bounds = (element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const stage = document.querySelector("#frame-stage");
    const canvas = document.querySelector("#scenario-frame");
    const svg = document.querySelector("#scenario-overlay");
    const imageBounds = bounds(canvas);
    const imageScale = Math.min(imageBounds.width / canvas.width, imageBounds.height / canvas.height);
    const imageContent = {
      x: imageBounds.x + (imageBounds.width - canvas.width * imageScale) / 2,
      y: imageBounds.y + (imageBounds.height - canvas.height * imageScale) / 2,
      width: canvas.width * imageScale,
      height: canvas.height * imageScale,
    };
    const svgBounds = bounds(svg);
    const viewBox = svg.viewBox.baseVal;
    const svgScale = Math.min(svgBounds.width / viewBox.width, svgBounds.height / viewBox.height);
    const svgContent = {
      x: svgBounds.x + (svgBounds.width - viewBox.width * svgScale) / 2,
      y: svgBounds.y + (svgBounds.height - viewBox.height * svgScale) / 2,
      width: viewBox.width * svgScale,
      height: viewBox.height * svgScale,
    };
    const target = document.querySelector(".overlay-box.target");
    return {
      image: imageBounds,
      imageContent,
      stage: bounds(stage),
      svg: svgBounds,
      svgContent,
      target: target ? bounds(target) : null,
    };
  });
}

test("image pixels and SVG annotations share one rendered coordinate space", async (context) => {
  const output = path.join(CONTROL_PLANE, "dist-test-browser-layout");
  context.after(async () => rm(output, { recursive: true, force: true }));
  const build = spawnSync(process.execPath, ["scripts/build-scenario-catalog.mjs", "--output", output], {
    cwd: CONTROL_PLANE,
    encoding: "utf8",
  });
  assert.equal(build.status, 0, `${build.stdout}\n${build.stderr}`);

  const fixture = await staticServer(output);
  context.after(fixture.close);
  const browser = await chromium.launch({ executablePath: await chromeExecutable(), headless: true });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  await loadScenario(page, fixture.origin, "synthetic-acquisition", 2);
  const synthetic = await frameGeometry(page);
  assertBoundsMatch(synthetic.image, synthetic.stage, "synthetic image layer");
  assertBoundsMatch(synthetic.svg, synthetic.stage, "synthetic SVG layer");
  assertBoundsMatch(synthetic.imageContent, synthetic.svgContent, "synthetic content transform");
  assert.ok(synthetic.target, "known synthetic target overlay must render");
  const scale = synthetic.imageContent.width / 160;
  assertBoundsMatch(synthetic.target, {
    x: synthetic.imageContent.x + 8 * scale,
    y: synthetic.imageContent.y + 40 * scale,
    width: 20 * scale,
    height: 26 * scale,
  }, "known [8,40,28,66] target overlay", 0.25);

  await loadScenario(page, fixture.origin, "rvmot-a1c9", 75);
  const realVideo = await frameGeometry(page);
  assertBoundsMatch(realVideo.image, realVideo.stage, "real-video image layer");
  assertBoundsMatch(realVideo.svg, realVideo.stage, "real-video SVG layer");
  assertBoundsMatch(realVideo.imageContent, realVideo.svgContent, "real-video content transform");
  assert.ok(realVideo.target, "dense real-video tracked-object overlay must render");
  assert.equal(await page.locator("#ignores-overlay-label").isHidden(), true);
  assert.equal(await page.locator("#region-overlay-label").isHidden(), true);
  assert.match(await page.locator("#overlay-disclosure").textContent(), /never these boxes, identities, annotations, or future frames/);

  const before = Number(await page.locator("#frame-scrubber").inputValue());
  await page.locator("#play-pause").click();
  await page.waitForFunction((value) => Number(document.querySelector("#frame-scrubber").value) >= value + 3, before);
  await page.locator("#play-pause").click();
  assert.equal(await page.locator("#playback-speed").inputValue(), "1");

  for (const width of [320, 390, 430]) {
    await page.setViewportSize({ width, height: 900 });
    await loadScenario(page, fixture.origin, "synthetic-acquisition", 2);
    const mobile = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth - innerWidth,
      controls: [...document.querySelectorAll(".viewer-controls button")].map((button) => {
        const rect = button.getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      }),
    }));
    assert.ok(mobile.overflow <= 0, `${width}px viewport must not overflow`);
    for (const control of mobile.controls) {
      assert.ok(control.width >= 44 && control.height >= 44, `${width}px viewer buttons must remain at least 44px`);
    }
  }
});

async function buildBrowserFixture(context, suffix, serverOptions = {}) {
  const output = path.join(CONTROL_PLANE, `dist-test-browser-${suffix}`);
  context.after(async () => rm(output, { recursive: true, force: true }));
  const build = spawnSync(process.execPath, ["scripts/build-scenario-catalog.mjs", "--output", output], {
    cwd: CONTROL_PLANE,
    encoding: "utf8",
  });
  assert.equal(build.status, 0, `${build.stdout}\n${build.stderr}`);
  const fixture = await staticServer(output, serverOptions);
  context.after(fixture.close);
  const browser = await chromium.launch({ executablePath: await chromeExecutable(), headless: true });
  context.after(() => browser.close());
  return { browser, fixture };
}

async function selectExactFrame(page, index) {
  await page.locator("#frame-scrubber").evaluate((scrubber, value) => {
    scrubber.value = String(value);
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  }, index);
  await page.waitForFunction((value) => {
    const canvas = document.querySelector("#scenario-frame");
    return Number(document.querySelector("#frame-scrubber").value) === value
      && canvas?.dataset.frameReady === "true"
      && canvas.getAttribute("aria-label")?.includes(`exact frame ${value + 1} of`);
  }, index);
}

async function samplePresentation(page, durationMs, intervalMs = 8) {
  return page.evaluate(async ({ durationMs: duration, intervalMs: interval }) => {
    const canvas = document.querySelector("#scenario-frame");
    const stage = document.querySelector("#frame-stage");
    const overlay = document.querySelector("#scenario-overlay");
    const mediaState = document.querySelector("#media-state");
    const initialBounds = stage.getBoundingClientRect();
    const samples = [];
    const started = performance.now();
    do {
      const bounds = stage.getBoundingClientRect();
      samples.push({
        blank: canvas.dataset.frameReady !== "true" || bounds.width === 0 || bounds.height === 0,
        canvasFrame: canvas.dataset.frameIndex,
        layoutDelta: Math.max(
          Math.abs(bounds.width - initialBounds.width),
          Math.abs(bounds.height - initialBounds.height),
        ),
        mediaClass: mediaState.className,
        mediaText: mediaState.textContent,
        mediaVisible: !mediaState.hidden,
        overlay: overlay.innerHTML,
      });
      await new Promise((resolve) => setTimeout(resolve, interval));
    } while (performance.now() - started < duration);
    return samples;
  }, { durationMs, intervalMs });
}

async function installBitmapAccounting(page) {
  await page.addInitScript(() => {
    const closedBitmaps = new WeakSet();
    const originalClose = ImageBitmap.prototype.close;
    const originalCreateImageBitmap = window.createImageBitmap;
    const probe = {
      closed: 0,
      created: 0,
      decode: { decodedBytes: 0, liveEntries: 0 },
      hold: false,
      pending: 0,
      releases: [],
      releaseAll() {
        for (const release of this.releases.splice(0)) release();
      },
    };
    window.__bitmapProbe = probe;
    window.__cvbenchReportDecodeState = (decode) => {
      probe.decode = decode;
    };
    ImageBitmap.prototype.close = function close() {
      if (!closedBitmaps.has(this)) {
        closedBitmaps.add(this);
        probe.closed += 1;
      }
      return Reflect.apply(originalClose, this, []);
    };
    window.createImageBitmap = async (...args) => {
      const bitmap = await Reflect.apply(originalCreateImageBitmap, window, args);
      probe.created += 1;
      if (probe.hold) {
        probe.pending += 1;
        await new Promise((resolve) => probe.releases.push(resolve));
        probe.pending -= 1;
      }
      return bitmap;
    };
  });
}

async function bitmapAccounting(page) {
  return page.evaluate(() => ({
    closed: window.__bitmapProbe.closed,
    created: window.__bitmapProbe.created,
    decodedBytes: window.__bitmapProbe.decode.decodedBytes,
    liveEntries: window.__bitmapProbe.decode.liveEntries,
    pending: window.__bitmapProbe.pending,
  }));
}

function assertBitmapOwnership(accounting, label) {
  assert.equal(accounting.pending, 0, `${label}: no decoded bitmap may remain suspended`);
  assert.equal(accounting.created, accounting.closed + accounting.liveEntries,
    `${label}: every created bitmap must be closed or owned by the live cache`);
  assert.ok(accounting.decodedBytes <= 48 * 1024 * 1024,
    `${label}: decoded cache must remain inside the 48 MiB budget`);
}

async function measureCadence(page, speed, durationMs = 700, fps = 30) {
  await selectExactFrame(page, 0);
  await page.locator("#playback-speed").selectOption(String(speed));
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent.startsWith("Playing from frame"));
  const start = await page.evaluate(() => ({
    frame: Number(document.querySelector("#frame-scrubber").value),
    time: performance.now(),
  }));
  await page.waitForTimeout(durationMs);
  await page.locator("#play-pause").click();
  const end = await page.evaluate(() => ({
    frame: Number(document.querySelector("#frame-scrubber").value),
    time: performance.now(),
  }));
  const sourceElapsedMs = (end.frame - start.frame) * (1000 / fps);
  const expectedElapsedMs = (end.time - start.time) * speed;
  return { ...end, driftMs: sourceElapsedMs - expectedElapsedMs, sourceElapsedMs, start };
}

test("native playback uses a monotonic source clock without blank or shifted presentation", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "cadence", { mediaDelay: 20 });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  await loadScenario(page, fixture.origin, "rvmot-a1c9");

  await page.evaluate(() => {
    const canvas = document.querySelector("#scenario-frame");
    const stage = document.querySelector("#frame-stage");
    const initial = stage.getBoundingClientRect();
    window.__visualProbe = {
      blankFrames: 0,
      maxLayoutDelta: 0,
      running: true,
      errorClassFrames: 0,
      visibleStatusFrames: 0,
      visibleStatusText: new Set(),
    };
    const sample = () => {
      if (canvas.dataset.frameReady !== "true") window.__visualProbe.blankFrames += 1;
      const mediaState = document.querySelector("#media-state");
      if (mediaState.classList.contains("error")) window.__visualProbe.errorClassFrames += 1;
      if (!mediaState.hidden) {
        window.__visualProbe.visibleStatusFrames += 1;
        window.__visualProbe.visibleStatusText.add(mediaState.textContent);
      }
      const current = stage.getBoundingClientRect();
      window.__visualProbe.maxLayoutDelta = Math.max(
        window.__visualProbe.maxLayoutDelta,
        Math.abs(current.width - initial.width),
        Math.abs(current.height - initial.height),
      );
      if (window.__visualProbe.running) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });

  for (const speed of [0.5, 1, 2]) {
    const measurement = await measureCadence(page, speed);
    assert.ok(measurement.sourceElapsedMs > 0, `${speed}x must advance the source clock`);
    assert.ok(Math.abs(measurement.driftMs) <= 70,
      `${speed}x source-clock drift ${measurement.driftMs.toFixed(1)}ms exceeds two 30 FPS frames`);
  }

  await selectExactFrame(page, 0);
  await page.locator("#playback-speed").selectOption("1");
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent.startsWith("Playing from frame"));
  const beforeBlock = Number(await page.locator("#frame-scrubber").inputValue());
  await page.evaluate(() => {
    const until = performance.now() + 220;
    while (performance.now() < until) {
      // Deliberately block presentation; the monotonic source clock must continue.
    }
  });
  await page.waitForFunction((frame) => Number(document.querySelector("#frame-scrubber").value) >= frame + 5, beforeBlock);
  await page.locator("#play-pause").click();

  const visual = await page.evaluate(() => {
    window.__visualProbe.running = false;
    return {
      ...window.__visualProbe,
      visibleStatusText: [...window.__visualProbe.visibleStatusText],
    };
  });
  assert.equal(visual.blankFrames, 0);
  assert.equal(visual.maxLayoutDelta, 0);
  assert.equal(visual.errorClassFrames, 0, "routine playback must not flicker the persistent-error class");
  assert.equal(visual.visibleStatusFrames, 0,
    `routine playback must never cover the verified frame; saw ${visual.visibleStatusText.join(", ")}`);
  assert.equal(await page.locator("#scenario-frame").getAttribute("role"), "img");
  assert.match(await page.locator("#scenario-frame").getAttribute("aria-label"), /exact frame \d+ of 150/);
  const synchronized = await page.evaluate(() => ({
    canvasHeight: document.querySelector("#scenario-frame").height,
    canvasWidth: document.querySelector("#scenario-frame").width,
    frame: Number(document.querySelector("#frame-scrubber").value) + 1,
    label: document.querySelector("#scenario-frame").getAttribute("aria-label"),
    viewBox: document.querySelector("#scenario-overlay").getAttribute("viewBox"),
  }));
  assert.equal(synchronized.label.includes(`exact frame ${synchronized.frame} of`), true);
  assert.equal(synchronized.viewBox, `0 0 ${synchronized.canvasWidth} ${synchronized.canvasHeight}`);
});

test("hash verification and transient buffering hold the last good presentation without visible progress", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "nonvisual-progress");
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "connection", { configurable: true, value: { saveData: true } });
    const originalDigest = SubtleCrypto.prototype.digest;
    const originalCreateImageBitmap = window.createImageBitmap;
    const probe = {
      holdDecode: false,
      holdDigest: false,
      decodePending: 0,
      decodeReleases: [],
      digestPending: 0,
      digestReleases: [],
      releaseDecodes() {
        for (const release of this.decodeReleases.splice(0)) release();
      },
      releaseDigests() {
        for (const release of this.digestReleases.splice(0)) release();
      },
    };
    window.__presentationProbe = probe;
    SubtleCrypto.prototype.digest = async function digest(...args) {
      if (probe.holdDigest) {
        probe.digestPending += 1;
        await new Promise((resolve) => probe.digestReleases.push(resolve));
        probe.digestPending -= 1;
      }
      return Reflect.apply(originalDigest, this, args);
    };
    window.createImageBitmap = async (...args) => {
      const bitmap = await Reflect.apply(originalCreateImageBitmap, window, args);
      if (probe.holdDecode) {
        probe.decodePending += 1;
        await new Promise((resolve) => probe.decodeReleases.push(resolve));
        probe.decodePending -= 1;
      }
      return bitmap;
    };
  });
  await loadScenario(page, fixture.origin, "rvmot-a1c9");

  const lastGood = await page.evaluate(() => ({
    frame: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  }));
  await page.evaluate(() => {
    window.__presentationProbe.holdDigest = true;
    const scrubber = document.querySelector("#frame-scrubber");
    scrubber.value = "100";
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => window.__presentationProbe.digestPending === 1);
  const verificationSamples = await samplePresentation(page, 160);
  assert.ok(verificationSamples.length >= 12, "verification must be sampled faster than a 60 FPS frame");
  assert.ok(verificationSamples.every((sample) => !sample.mediaVisible
    && sample.mediaClass === "media-state"
    && sample.mediaText === ""
    && sample.canvasFrame === lastGood.frame
    && sample.overlay === lastGood.overlay
    && sample.layoutDelta === 0
    && !sample.blank),
  "delayed SHA verification must leave the last good pixels and overlay visually untouched");
  assert.deepEqual(await page.evaluate(() => ({
    frame: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  })), lastGood, "delayed verification must not redraw the last good presentation");
  await page.evaluate(() => {
    window.__presentationProbe.holdDigest = false;
    window.__presentationProbe.releaseDigests();
  });
  await selectExactFrame(page, 100);

  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent.startsWith("Playing from frame"));
  await page.evaluate(() => { window.__presentationProbe.holdDecode = true; });
  await page.waitForFunction(() => window.__presentationProbe.decodePending > 0);
  await page.waitForTimeout(300);
  assert.notEqual(await page.locator("#viewer-announcement").textContent(),
    "Playback buffering. The current frame remains visible.",
  "short decode stalls must not immediately spam the assistive live region");
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent ===
    "Playback buffering. The current frame remains visible.");
  const bufferingGood = await page.evaluate(() => ({
    frame: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  }));
  const bufferingSamples = await samplePresentation(page, 160);
  assert.ok(bufferingSamples.length >= 12, "buffering must be sampled faster than a 60 FPS frame");
  assert.ok(bufferingSamples.every((sample) => !sample.mediaVisible
    && sample.mediaClass === "media-state"
    && sample.mediaText === ""
    && sample.canvasFrame === bufferingSamples[0].canvasFrame
    && sample.overlay === bufferingSamples[0].overlay
    && sample.layoutDelta === 0
    && !sample.blank),
  "transient buffering must leave the last good pixels and overlay visually untouched");
  assert.deepEqual(await page.evaluate(() => ({
    frame: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  })), bufferingGood, "transient buffering must not redraw the last good presentation");
  await page.evaluate(() => {
    window.__presentationProbe.holdDecode = false;
    window.__presentationProbe.releaseDecodes();
  });
  await page.waitForFunction((frame) => Number(document.querySelector("#scenario-frame").dataset.frameIndex) > Number(frame),
    bufferingSamples[0].canvasFrame);
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent === "Playback resumed.");
  await page.locator("#play-pause").click();
  context.diagnostic(`sub-frame probes: ${verificationSamples.length} delayed-verification samples and `
    + `${bufferingSamples.length} buffering samples, zero visible status/blank/layout changes`);
});

test("real-browser playback follows exact 24, 30, and 60 FPS source timestamps", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "source-rates");
  for (const fps of [24, 30, 60]) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.route("**/scenario-catalog/v1/frame-manifests/**", async (route) => {
      const response = await route.fetch();
      const manifest = await response.json();
      manifest.frames = manifest.frames.map((frame, index) => ({
        ...frame,
        source_timestamp_ns: Math.round(index * 1_000_000_000 / fps),
      }));
      await route.fulfill({ response, json: manifest });
    });
    await loadScenario(page, fixture.origin, "rvmot-a1c9");
    const measurement = await measureCadence(page, 1, 650, fps);
    assert.ok(Math.abs(measurement.driftMs) <= 1000 / fps * 2,
      `${fps} FPS source-clock drift ${measurement.driftMs.toFixed(1)}ms exceeds two source frames`);
    await page.close();
  }
});

test("playback controls retain exact-frame inspection, synchronized overlays, and accessible state", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "controls");
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await loadScenario(page, fixture.origin, "rvmot-a1c9", 20);

  await page.locator("#play-pause").click();
  await page.waitForFunction(() => Number(document.querySelector("#frame-scrubber").value) >= 23);
  await page.locator("#play-pause").click();
  const paused = Number(await page.locator("#frame-scrubber").inputValue());
  await page.waitForTimeout(150);
  assert.equal(Number(await page.locator("#frame-scrubber").inputValue()), paused);
  assert.match(await page.locator("#viewer-announcement").textContent(), /Paused on frame/);

  await page.locator("#play-pause").click();
  await page.waitForFunction((frame) => Number(document.querySelector("#frame-scrubber").value) > frame, paused);
  await selectExactFrame(page, 60);
  assert.equal(await page.locator("#play-pause").textContent(), "Play");
  assert.match(await page.locator("#scenario-frame").getAttribute("aria-label"), /exact frame 61 of 150/);
  assert.equal(Number(await page.locator("#frame-scrubber").inputValue()), 60);

  const targetToggle = page.locator('[data-overlay="targets"]');
  assert.ok(await page.locator(".overlay-box.target").count() > 0);
  await targetToggle.uncheck();
  assert.equal(await page.locator(".overlay-box.target").count(), 0);
  await targetToggle.check();
  assert.ok(await page.locator(".overlay-box.target").count() > 0);

  await page.locator("#frame-viewer").focus();
  await page.locator("#frame-viewer").press("ArrowRight");
  await page.waitForFunction(() => Number(document.querySelector("#frame-scrubber").value) === 61);
  await page.locator("#frame-viewer").press("ArrowLeft");
  await page.waitForFunction(() => Number(document.querySelector("#frame-scrubber").value) === 60);
  await page.locator("#frame-viewer").press("Space");
  await page.waitForFunction(() => document.querySelector("#play-pause").textContent === "Pause");
  await page.locator("#frame-viewer").press("Space");
  assert.equal(await page.locator("#play-pause").textContent(), "Play");

  await selectExactFrame(page, 145);
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent === "Playback ended on frame 150.");
  assert.equal(Number(await page.locator("#frame-scrubber").inputValue()), 149);
  assert.equal(await page.locator("#play-pause").getAttribute("aria-label"), "Play sequence");
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#play-pause").textContent === "Pause"
    && Number(document.querySelector("#frame-scrubber").value) >= 2
    && Number(document.querySelector("#frame-scrubber").value) < 149);
  assert.equal(await page.locator("#play-pause").textContent(), "Pause");
  await page.locator("#play-pause").click();
});

test("final-frame restart preserves persistent frame-zero failures instead of reporting normal end", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "endpoint-restart-failures");
  const detail = await (await fetch(`${fixture.origin}/scenario-catalog/v1/scenarios/rvmot-a1c9.json`)).json();
  const manifest = await (await fetch(new URL(detail.media.frame_manifest.url, fixture.origin))).json();
  const frameZeroPath = new URL(manifest.frames[0].media.url, fixture.origin).pathname;
  const failures = [
    {
      body: "missing exact frame",
      contentType: "text/plain",
      expected: "Exact frame is missing (404).",
      label: "404",
      status: 404,
    },
    {
      body: "temporarily unavailable",
      contentType: "text/plain",
      expected: "Exact frame is unavailable because retrieval failed (503).",
      label: "503",
      status: 503,
    },
    {
      body: "not the published JPEG bytes",
      contentType: "image/jpeg",
      expected: "Exact frame failed its published SHA-256 check.",
      label: "corrupt SHA",
      status: 200,
    },
  ];

  for (const failure of failures) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    let failRestart = false;
    let restartRequests = 0;
    await page.route("**/*.jpg", async (route) => {
      if (!failRestart || new URL(route.request().url()).pathname !== frameZeroPath) {
        await route.continue();
        return;
      }
      restartRequests += 1;
      await route.fulfill({
        body: failure.body,
        contentType: failure.contentType,
        status: failure.status,
      });
    });
    await loadScenario(page, fixture.origin, "rvmot-a1c9");
    await page.locator("#playback-speed").selectOption("2");
    await page.locator("#play-pause").click();
    await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent ===
      "Playback ended on frame 150.");
    assert.equal(Number(await page.locator("#frame-scrubber").inputValue()), 149);

    const lastGood = await page.evaluate(() => ({
      frame: document.querySelector("#scenario-frame").dataset.frameIndex,
      overlay: document.querySelector("#scenario-overlay").innerHTML,
      pixels: document.querySelector("#scenario-frame").toDataURL(),
    }));
    await page.evaluate(() => {
      const probe = {
        samples: [],
        timer: null,
      };
      const sample = () => {
        const mediaState = document.querySelector("#media-state");
        probe.samples.push({
          announcement: document.querySelector("#viewer-announcement").textContent,
          error: mediaState.classList.contains("error"),
          frame: document.querySelector("#scenario-frame").dataset.frameIndex,
          hidden: mediaState.hidden,
          text: mediaState.textContent,
        });
      };
      sample();
      probe.timer = setInterval(sample, 2);
      window.__endpointRestartProbe = probe;
    });
    failRestart = true;
    await page.locator("#play-pause").click();
    await page.waitForTimeout(160);
    const result = await page.evaluate(() => {
      clearInterval(window.__endpointRestartProbe.timer);
      const mediaState = document.querySelector("#media-state");
      return {
        announcement: document.querySelector("#viewer-announcement").textContent,
        error: mediaState.classList.contains("error"),
        frame: document.querySelector("#scenario-frame").dataset.frameIndex,
        hidden: mediaState.hidden,
        overlay: document.querySelector("#scenario-overlay").innerHTML,
        pixels: document.querySelector("#scenario-frame").toDataURL(),
        playButton: document.querySelector("#play-pause").textContent,
        samples: window.__endpointRestartProbe.samples,
        text: mediaState.textContent,
      };
    });

    const firstErrorSample = result.samples.findIndex((sample) => sample.error && !sample.hidden);
    assert.ok(firstErrorSample >= 0, `${failure.label}: the persistent frame-zero failure must become visible`);
    assert.ok(result.samples.slice(firstErrorSample).every((sample) =>
      sample.error
      && !sample.hidden
      && sample.text === failure.expected
      && sample.frame === "149"
      && sample.announcement === failure.expected),
    `${failure.label}: every sub-frame sample after failure must retain the actual error`);
    assert.equal(result.hidden, false, `${failure.label}: the actionable error must remain visible`);
    assert.equal(result.error, true, `${failure.label}: the persistent error class must remain set`);
    assert.equal(result.text, failure.expected);
    assert.equal(result.announcement, failure.expected,
      `${failure.label}: accessibility state must announce the failure, not normal playback end`);
    assert.equal(result.playButton, "Play", `${failure.label}: failed restart must leave playback stopped`);
    assert.deepEqual({
      frame: result.frame,
      overlay: result.overlay,
      pixels: result.pixels,
    }, lastGood, `${failure.label}: failed restart must preserve the final verified frame and overlay`);
    assert.equal(restartRequests, 1, `${failure.label}: restart must make one fresh frame-zero request`);
    context.diagnostic(`${failure.label} endpoint restart: ${result.samples.length} samples on a requested 2ms cadence; `
      + `persistent error, final-frame pixels/overlay, and actual announcement preserved`);
    await page.close();
  }
});

test("a delayed stale playback failure cannot stop a scrubbed and resumed session", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "stale-playback-error");
  const detail = await (await fetch(`${fixture.origin}/scenario-catalog/v1/scenarios/rvmot-a1c9.json`)).json();
  const manifest = await (await fetch(new URL(detail.media.frame_manifest.url, fixture.origin))).json();
  const staleIndex = 20;
  const stalePath = new URL(manifest.frames[staleIndex].media.url, fixture.origin).pathname;
  const staleBody = "stale playback session A";
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.addInitScript((marker) => {
    const originalDigest = SubtleCrypto.prototype.digest;
    const probe = {
      pending: 0,
      releases: [],
      releaseAll() {
        for (const release of this.releases.splice(0)) release();
      },
    };
    window.__staleDigestProbe = probe;
    SubtleCrypto.prototype.digest = async function digest(algorithm, data) {
      if (new TextDecoder().decode(new Uint8Array(data)) === marker) {
        probe.pending += 1;
        await new Promise((resolve) => probe.releases.push(resolve));
        probe.pending -= 1;
      }
      return Reflect.apply(originalDigest, this, [algorithm, data]);
    };
  }, staleBody);
  await page.route("**/*.jpg", async (route) => {
    if (new URL(route.request().url()).pathname === stalePath) {
      await route.fulfill({ body: staleBody, contentType: "image/jpeg", status: 200 });
      return;
    }
    await route.continue();
  });

  await loadScenario(page, fixture.origin, "rvmot-a1c9");
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent.startsWith("Playing from frame"));
  await page.waitForFunction(() => window.__staleDigestProbe.pending === 1);

  await selectExactFrame(page, 100);
  await page.locator("#play-pause").click();
  await page.waitForFunction(() => document.querySelector("#viewer-announcement")?.textContent.startsWith("Playing from frame 101"));
  await page.waitForFunction(() => Number(document.querySelector("#frame-scrubber").value) > 102);
  const beforeRelease = await page.evaluate(() => ({
    announcement: document.querySelector("#viewer-announcement").textContent,
    frame: Number(document.querySelector("#frame-scrubber").value),
    mediaError: document.querySelector("#media-state").classList.contains("error"),
    mediaHidden: document.querySelector("#media-state").hidden,
  }));

  await page.evaluate(() => window.__staleDigestProbe.releaseAll());
  await page.waitForFunction(() => window.__staleDigestProbe.pending === 0);
  await page.waitForTimeout(150);
  const afterRelease = await page.evaluate(() => ({
    announcement: document.querySelector("#viewer-announcement").textContent,
    frame: Number(document.querySelector("#frame-scrubber").value),
    mediaError: document.querySelector("#media-state").classList.contains("error"),
    mediaHidden: document.querySelector("#media-state").hidden,
    playButton: document.querySelector("#play-pause").textContent,
  }));

  assert.equal(afterRelease.playButton, "Pause", "stale session A must not stop playback session B");
  assert.equal(afterRelease.mediaError, false, "stale session A must not surface a media error");
  assert.equal(afterRelease.mediaHidden, true, "session B must retain its normal hidden media status");
  assert.equal(afterRelease.announcement, beforeRelease.announcement,
    "stale session A must not replace session B's accessibility announcement");
  assert.ok(afterRelease.frame > beforeRelease.frame, "session B must keep advancing after the stale failure");
  context.diagnostic(`stale session A digest failed after session B advanced `
    + `from frame ${beforeRelease.frame} to ${afterRelease.frame}; playback and announcement remained active`);
});

test("cache-cold navigation, Save-Data, reduced motion, and mobile rendering stay bounded", async (context) => {
  const mediaRequests = [];
  const { browser, fixture } = await buildBrowserFixture(context, "policies", { mediaDelay: 15, mediaRequests });
  const page = await browser.newPage({ viewport: { width: 390, height: 900 } });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "connection", { configurable: true, value: { saveData: true } });
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await loadScenario(page, fixture.origin, "rvmot-a1c9");
  await page.waitForTimeout(150);
  assert.equal(new Set(mediaRequests).size, 1, "Save-Data must keep paused poster loading to one exact frame");
  assert.equal(await page.locator("#play-pause").textContent(), "Play");
  assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).scrollBehavior), "auto");

  await page.locator("#play-pause").click();
  await page.waitForFunction(() => Number(document.querySelector("#frame-scrubber").value) >= 3);
  await page.locator("#play-pause").click();
  assert.ok(new Set(mediaRequests).size < 20, "Save-Data playback must keep a small active-scenario window");

  await page.route("**/scenario-catalog/v1/scenarios/rvmot-a1c9.json", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 180));
    await route.continue().catch(() => {});
  });
  await page.goto(`${fixture.origin}/scenarios/`);
  await page.locator("#scenario-list .scenario-card").first().waitFor();
  await page.evaluate(() => {
    history.pushState({}, "", "/scenarios/?scenario=rvmot-a1c9");
    dispatchEvent(new PopStateEvent("popstate"));
    history.pushState({}, "", "/scenarios/?scenario=rvmot-b7e2");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await page.waitForFunction(() => document.querySelector("#detail-kicker")?.textContent.includes("rvmot-b7e2"));
  await page.waitForTimeout(220);
  assert.match(await page.locator("#detail-kicker").textContent(), /rvmot-b7e2/);
  const requestsBeforeRapidScrub = mediaRequests.length;
  await page.locator("#frame-scrubber").evaluate((scrubber) => {
    for (const index of [20, 50, 80, 100]) {
      scrubber.value = String(index);
      scrubber.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  await page.waitForFunction(() => document.querySelector("#scenario-frame").dataset.frameIndex === "100");
  assert.ok(mediaRequests.length - requestsBeforeRapidScrub <= 4,
    "rapid scrubbing must cancel superseded exact-frame network work");

  for (const width of [320, 390, 430]) {
    await page.setViewportSize({ width, height: 900 });
    const mobile = await page.evaluate(() => ({
      canvasReady: document.querySelector("#scenario-frame").dataset.frameReady,
      overflow: document.documentElement.scrollWidth - innerWidth,
      stageHeight: document.querySelector("#frame-stage").getBoundingClientRect().height,
    }));
    assert.equal(mobile.canvasReady, "true");
    assert.ok(mobile.overflow <= 0, `${width}px viewport must not overflow`);
    assert.ok(mobile.stageHeight > 0, `${width}px frame stage must remain visible`);
  }
});

test("rapid scrubbing and scenario navigation close every superseded decoded bitmap", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "bitmap-ownership");
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await installBitmapAccounting(page);
  await loadScenario(page, fixture.origin, "rvmot-a1c9");
  await page.waitForTimeout(200);
  assertBitmapOwnership(await bitmapAccounting(page), "initial scenario");

  await page.evaluate(() => {
    window.__bitmapProbe.hold = true;
    const scrubber = document.querySelector("#frame-scrubber");
    scrubber.value = "60";
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => window.__bitmapProbe.pending >= 1);
  await page.locator("#frame-scrubber").evaluate((scrubber) => {
    scrubber.value = "90";
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => window.__bitmapProbe.pending >= 2);
  await page.evaluate(() => {
    window.__bitmapProbe.hold = false;
    window.__bitmapProbe.releaseAll();
  });
  await page.waitForFunction(() => document.querySelector("#scenario-frame").dataset.frameIndex === "90");
  await page.waitForTimeout(200);
  await page.waitForFunction(() => {
    const probe = window.__bitmapProbe;
    return probe.pending === 0 && probe.created === probe.closed + probe.decode.liveEntries;
  });
  const rapidScrubAccounting = await bitmapAccounting(page);
  assertBitmapOwnership(rapidScrubAccounting, "rapid scrub");
  context.diagnostic(`rapid scrub bitmaps: ${rapidScrubAccounting.created} created = `
    + `${rapidScrubAccounting.closed} closed + ${rapidScrubAccounting.liveEntries} live; `
    + `${rapidScrubAccounting.decodedBytes} decoded bytes`);

  await page.route("**/scenario-catalog/v1/scenarios/rvmot-b7e2.json", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.continue();
  });
  await page.evaluate(() => {
    window.__bitmapProbe.hold = true;
    const scrubber = document.querySelector("#frame-scrubber");
    scrubber.value = "120";
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => window.__bitmapProbe.pending >= 1);
  await page.evaluate(() => {
    history.pushState({}, "", "/scenarios/?scenario=rvmot-b7e2");
    dispatchEvent(new PopStateEvent("popstate"));
    window.__bitmapProbe.hold = false;
    window.__bitmapProbe.releaseAll();
  });
  await page.waitForFunction(() => document.querySelector("#detail-kicker")?.textContent.includes("rvmot-b7e2"));
  await page.waitForFunction(() => document.querySelector("#scenario-frame").dataset.frameIndex === "0");
  await page.waitForTimeout(200);
  await page.waitForFunction(() => {
    const probe = window.__bitmapProbe;
    return probe.pending === 0 && probe.created === probe.closed + probe.decode.liveEntries;
  });
  const navigationAccounting = await bitmapAccounting(page);
  assertBitmapOwnership(navigationAccounting, "scenario navigation");
  context.diagnostic(`scenario navigation bitmaps: ${navigationAccounting.created} created = `
    + `${navigationAccounting.closed} closed + ${navigationAccounting.liveEntries} live; `
    + `${navigationAccounting.decodedBytes} decoded bytes`);
});

test("a one-time 503 retries the exact frame without blanking or reloading the scenario", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "transient-frame-error");
  const detail = await (await fetch(`${fixture.origin}/scenario-catalog/v1/scenarios/rvmot-a1c9.json`)).json();
  const manifest = await (await fetch(new URL(detail.media.frame_manifest.url, fixture.origin))).json();
  const targetIndex = 100;
  const targetPath = new URL(manifest.frames[targetIndex].media.url, fixture.origin).pathname;
  let failOnce = true;
  let targetRequests = 0;
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.route("**/*.jpg", async (route) => {
    if (new URL(route.request().url()).pathname !== targetPath) {
      await route.continue();
      return;
    }
    targetRequests += 1;
    if (failOnce) {
      failOnce = false;
      await route.fulfill({ body: "temporary outage", contentType: "text/plain", status: 503 });
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 180));
    await route.continue();
  });
  await loadScenario(page, fixture.origin, "rvmot-a1c9");
  const initialUrl = page.url();
  const visibleBefore = await page.evaluate(() => ({
    frameIndex: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  }));

  await page.locator("#frame-scrubber").evaluate((scrubber, index) => {
    scrubber.value = String(index);
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  }, targetIndex);
  await page.locator("#media-state.error").waitFor();
  assert.equal(await page.locator("#media-state").textContent(),
    "Exact frame is unavailable because retrieval failed (503).");
  const visibleAfterFailure = await page.evaluate(() => ({
    frameIndex: document.querySelector("#scenario-frame").dataset.frameIndex,
    overlay: document.querySelector("#scenario-overlay").innerHTML,
    pixels: document.querySelector("#scenario-frame").toDataURL(),
  }));
  assert.deepEqual(visibleAfterFailure, visibleBefore,
    "the honest transient error must preserve the last good frame and synchronized overlay");

  await page.locator("#frame-scrubber").evaluate((scrubber, index) => {
    scrubber.value = String(index);
    scrubber.dispatchEvent(new Event("input", { bubbles: true }));
  }, targetIndex);
  const retrySamples = await samplePresentation(page, 120);
  assert.ok(retrySamples.every((sample) => !sample.mediaVisible
    && sample.mediaClass === "media-state"
    && sample.mediaText === ""
    && sample.canvasFrame === visibleBefore.frameIndex
    && sample.overlay === visibleBefore.overlay
    && sample.layoutDelta === 0
    && !sample.blank),
  "retry verification must preserve and never cover the last good presentation");
  assert.equal(await page.locator("#scenario-frame").evaluate((canvas) => canvas.toDataURL()), visibleBefore.pixels,
    "retry verification must not redraw the last good pixels before success");
  await page.waitForFunction((value) => {
    const canvas = document.querySelector("#scenario-frame");
    return Number(document.querySelector("#frame-scrubber").value) === value
      && canvas?.dataset.frameReady === "true"
      && canvas.getAttribute("aria-label")?.includes(`exact frame ${value + 1} of`);
  }, targetIndex);
  assert.equal(page.url(), initialUrl, "retry must not reload or replace the scenario");
  assert.equal(targetRequests, 2, "the failed exact frame must be fetched again");
  assert.equal(await page.locator("#media-state").isHidden(), true);
  context.diagnostic(`transient frame ${targetIndex}: one 503, ${targetRequests} requests, `
    + `${retrySamples.length} sub-frame retry samples preserved visible frame ${visibleBefore.frameIndex}, `
    + `retry presented exact frame ${targetIndex}`);
});

test("missing and corrupt exact media keep an honest non-blank failure state", async (context) => {
  const { browser, fixture } = await buildBrowserFixture(context, "media-failures");

  const missing = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await missing.route("**/*.jpg", (route) => route.fulfill({
    body: "Not found",
    contentType: "text/plain",
    status: 404,
  }));
  await missing.goto(`${fixture.origin}/scenarios/?scenario=rvmot-a1c9`);
  await missing.locator("#media-state.error").waitFor();
  assert.equal(await missing.locator("#media-state").textContent(), "Exact frame is missing (404).");
  assert.equal(await missing.locator("#scenario-frame").getAttribute("data-frame-ready"), "false");
  assert.ok(await missing.locator("#frame-stage").evaluate((stage) => stage.getBoundingClientRect().height > 0));

  const corrupt = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await corrupt.route("**/*.jpg", (route) => route.fulfill({
    body: "not the published JPEG bytes",
    contentType: "image/jpeg",
    status: 200,
  }));
  await corrupt.goto(`${fixture.origin}/scenarios/?scenario=rvmot-a1c9`);
  await corrupt.locator("#media-state.error").waitFor();
  assert.equal(await corrupt.locator("#media-state").textContent(), "Exact frame failed its published SHA-256 check.");
  assert.equal(await corrupt.locator("#scenario-frame").getAttribute("data-frame-ready"), "false");
});
