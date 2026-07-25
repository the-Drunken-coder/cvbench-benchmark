#!/usr/bin/env node

import { spawn } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTROL_PLANE = path.resolve(HERE, "..");
const ROOT = path.resolve(CONTROL_PLANE, "..");
const temporaryRoot = await mkdtemp(path.join(tmpdir(), "cvbench-production-install-"));

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env: process.env, stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code) => code === 0 ? resolve() : reject(new Error(`${command} exited with ${code}`)));
  });
}

try {
  for (const relative of ["benchmarks", "scenario-catalog", "scenarios"]) {
    await cp(path.join(ROOT, relative), path.join(temporaryRoot, relative), { recursive: true });
  }
  await mkdir(path.join(temporaryRoot, "examples"), { recursive: true });
  await cp(path.join(ROOT, "examples/Dockerfile.real-video-prep"), path.join(temporaryRoot, "examples/Dockerfile.real-video-prep"));
  const stagedControlPlane = path.join(temporaryRoot, "control-plane");
  await mkdir(path.join(stagedControlPlane, "scripts"), { recursive: true });
  await cp(path.join(CONTROL_PLANE, "package.json"), path.join(stagedControlPlane, "package.json"));
  await cp(path.join(CONTROL_PLANE, "package-lock.json"), path.join(stagedControlPlane, "package-lock.json"));
  await cp(path.join(CONTROL_PLANE, "public"), path.join(stagedControlPlane, "public"), { recursive: true });
  await cp(path.join(CONTROL_PLANE, "scripts/build-scenario-catalog.mjs"), path.join(stagedControlPlane, "scripts/build-scenario-catalog.mjs"));
  await run("npm", ["ci", "--omit=dev"], stagedControlPlane);
  const catalog = JSON.parse(await readFile(path.join(stagedControlPlane, "dist/scenario-catalog/v1/catalog.json"), "utf8"));
  if (catalog.scenario_count !== 16) throw new Error("production-only install did not build the complete catalog");
  process.stdout.write("production-only npm install built all 16 scenarios\n");
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
