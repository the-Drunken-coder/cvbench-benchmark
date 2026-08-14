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
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".mp4", "video/mp4"],
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

function staticServer() {
  return http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
      if (pathname === "/favicon.ico") {
        response.writeHead(204);
        response.end();
        return;
      }
      let filename = path.resolve(DIST, `.${pathname === "/" ? "/index.html" : pathname}`);
      if (!filename.startsWith(`${DIST}${path.sep}`)) throw new Error("path escape");
      if ((await stat(filename)).isDirectory()) filename = path.join(filename, "index.html");
      const body = await readFile(filename);
      response.writeHead(200, { "content-type": TYPES.get(path.extname(filename)) ?? "application/octet-stream" });
      response.end(body);
    } catch {
      response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
      response.end("Not found");
    }
  });
}

test("table-first control pane keeps datasets, submission, and runs focused", async () => {
  const server = staticServer();
  let browser;
  let listening = false;

  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    listening = true;
    browser = await chromium.launch({ executablePath: await chromeExecutable(), headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    const origin = `http://127.0.0.1:${server.address().port}`;

    await page.goto(`${origin}/datasets/`);
    await page.getByRole("heading", { name: "Datasets", exact: true }).waitFor();
    await page.getByRole("heading", { name: "Recovered clean videos training proposals" }).waitFor();
    assert.equal(await page.locator(".dataset-table tbody tr").count(), 2);
    assert.match(await page.locator(".repository-status").textContent(), /847b9c2/);

    await page.getByRole("button", { name: "Minimal synthetic certification fixture" }).click();
    await page.getByRole("heading", { name: "Minimal synthetic certification fixture" }).waitFor();
    assert.equal(await page.locator("#clip-video").isHidden(), true);
    await page.getByText("No browser preview is published for this clip.").waitFor();
    await page.getByLabel("Search").fill("ravine");
    assert.equal(await page.locator(".dataset-table tbody tr").count(), 1);
    await page.getByRole("button", { name: "Ravine" }).click();
    await page.getByRole("heading", { name: "Ravine", exact: true }).waitFor();
    assert.match(await page.locator(".clip-detail").textContent(), /1,467/);
    assert.match(await page.locator(".clip-detail").textContent(), /YOLOX-X/);
    const clipVideo = page.locator("#clip-video");
    await page.getByText("Browser preview · 49 seconds").waitFor();
    assert.match(await clipVideo.getAttribute("src"), /pixabay-28855-ravine\.04dbf8f38f3b\.mp4$/);
    await page.waitForFunction(() => document.querySelector("#clip-video")?.readyState >= HTMLMediaElement.HAVE_METADATA);
    assert.ok(await clipVideo.evaluate((video) => video.duration > 48 && video.duration < 50));
    await page.getByLabel("Search").fill("no matching dataset");
    assert.equal(await page.locator(".dataset-table tbody tr").count(), 0);
    assert.equal(await page.locator("#dataset-detail").isHidden(), true);
    assert.equal(await clipVideo.getAttribute("src"), null);

    await page.getByRole("link", { name: "Submit", exact: true }).click();
    await page.waitForURL(`${origin}/submit/`);
    await page.getByText("cvbench submit . --wait --json", { exact: true }).waitFor();
    assert.equal(await page.locator(".manual-submit").getAttribute("open"), null);
    await page.getByText("Submit an existing registry image", { exact: true }).click();
    await page.getByLabel("Public image digest").fill("mutable:latest");
    await page.getByLabel("System name").fill("Example system");
    await page.getByLabel("System version").fill("1.0");
    await page.getByLabel("Submission API key").fill("temporary-key");
    await page.getByRole("button", { name: "Queue system" }).click();
    await page.getByText(/Mutable tags cannot be queued/).waitFor();

    await page.getByRole("link", { name: "Runs", exact: true }).click();
    await page.waitForURL(`${origin}/runs/`);
    await page.getByLabel("Submission UUID").fill("not-a-run");
    await page.getByRole("button", { name: "Open run" }).click();
    await page.getByText("Enter the complete submission UUID.").waitFor();

    await page.setViewportSize({ width: 580, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true);
    assert.deepEqual(consoleErrors, []);
  } finally {
    try {
      await browser?.close();
    } finally {
      if (listening) await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
  }
});
