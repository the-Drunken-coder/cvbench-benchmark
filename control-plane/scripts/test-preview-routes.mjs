#!/usr/bin/env node

import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CONTROL_PLANE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = await availablePort();
const origin = `http://127.0.0.1:${port}`;
const wrangler = path.join(CONTROL_PLANE, "node_modules/.bin/wrangler");
const child = spawn(wrangler, ["dev", "--local", "--ip", "127.0.0.1", "--port", String(port)], {
  cwd: CONTROL_PLANE,
  env: process.env,
  stdio: ["ignore", "pipe", "pipe"],
});
let diagnostics = "";
child.stdout.on("data", (chunk) => { diagnostics += chunk.toString(); });
child.stderr.on("data", (chunk) => { diagnostics += chunk.toString(); });

function availablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const selected = server.address().port;
      server.close(() => resolve(selected));
    });
  });
}

async function request(pathname) {
  return fetch(`${origin}${pathname}`, { signal: AbortSignal.timeout(5_000) });
}

async function ready() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await request("/scenario-catalog/v1/catalog.json");
      if (response.ok) return;
    } catch {
      // Wrangler is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Wrangler preview did not start.\n${diagnostics}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

try {
  await ready();
  const catalogResponse = await request("/scenario-catalog/v1/catalog.json");
  assert(catalogResponse.status === 200, "catalog preview route must return 200");
  assert(catalogResponse.headers.get("content-type")?.includes("application/json"), "catalog preview route must return JSON");
  const catalogEtag = catalogResponse.headers.get("etag");
  const catalog = await catalogResponse.json();
  if (catalogEtag) {
    const revalidatedCatalog = await fetch(`${origin}/scenario-catalog/v1/catalog.json`, { headers: { "if-none-match": catalogEtag } });
    assert(revalidatedCatalog.status === 304, "catalog conditional revalidation must remain 304");
  }
  const frameScenario = catalog.scenarios.find(({ id }) => id === "synthetic-acquisition");
  assert(frameScenario, "catalog must retain the exact-frame synthetic smoke scenario");
  const detailResponse = await request(frameScenario.detail.url);
  const detail = await detailResponse.json();
  const frameManifest = await (await request(detail.media.frame_manifest.url)).json();
  const frameResponse = await request(frameManifest.frames[0].media.url);
  assert(frameResponse.status === 200, "declared frame preview route must return 200");
  assert(frameResponse.headers.get("content-type") === "image/jpeg", "declared frame preview route must return image/jpeg");
  assert(frameResponse.headers.get("cache-control")?.includes("immutable"), "declared frame preview route must be immutable");

  const motScenario = catalog.scenarios.find(({ id }) => id === "mot17-02");
  assert(motScenario, "catalog must include the MOTChallenge smoke scenario");
  const motDetail = await (await request(motScenario.detail.url)).json();
  const videoResponse = await request(motDetail.media.viewer_derivative.url);
  assert(videoResponse.status === 200, "declared MOTChallenge video route must return 200");
  assert(videoResponse.headers.get("content-type") === "video/mp4", "declared MOTChallenge video route must return video/mp4");
  assert(videoResponse.headers.get("cache-control")?.includes("immutable"), "declared MOTChallenge video route must be immutable");
  const annotationsResponse = await request(motDetail.annotations.normalized_ground_truth.url);
  assert(annotationsResponse.status === 200, "declared MOTChallenge annotation bundle route must return 200");
  assert(annotationsResponse.headers.get("content-type") === "application/gzip", "declared MOTChallenge annotation bundle must return application/gzip");

  const missingJson = await request("/scenario-catalog/v1/scenarios/not-present.json");
  assert(missingJson.status === 404, "unknown scenario JSON must return 404");
  assert(missingJson.headers.get("content-type")?.includes("application/json"), "unknown scenario JSON must not return HTML");
  assert(!missingJson.headers.get("cache-control")?.includes("immutable"), "unknown scenario JSON must not be immutable");
  const missingFrame = await request(`/scenario-catalog/v1/assets/sha256/${"f".repeat(64)}.jpg`);
  assert(missingFrame.status === 404, "unknown frame must return 404");
  assert(missingFrame.headers.get("content-type") === "image/jpeg", "unknown frame must retain JPEG MIME");
  assert(!missingFrame.headers.get("cache-control")?.includes("immutable"), "unknown frame must not be immutable");
  const navigation = await request(`/scenarios/?scenario=${motScenario.id}`);
  assert(navigation.status === 200 && navigation.headers.get("content-type")?.includes("text/html"), "scenario navigation must remain available");
  const unknownPage = await request("/not-a-real-page");
  assert(unknownPage.status === 404, "unknown navigation must return the 404 page");
  process.stdout.write("Worker preview routes preserve navigation and honest catalog/media failures\n");
} finally {
  child.kill("SIGTERM");
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
}
