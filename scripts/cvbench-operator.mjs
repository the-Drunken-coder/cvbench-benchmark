#!/usr/bin/env node

import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

export function pollIntervalMs(rawValue) {
  if (rawValue === undefined || rawValue === null || String(rawValue).trim() === "") return 5000;
  const parsed = Number(rawValue ?? 5000);
  return Number.isFinite(parsed) ? Math.max(1000, parsed) : 5000;
}

async function main() {
  const [command = "list", value] = process.argv.slice(2);
  const baseUrl = required("CVBENCH_API_BASE_URL").replace(/\/$/, "");
  const token = required("CVBENCH_OPERATOR_READ_TOKEN");
  let lastBody;

  if (command === "watch") {
    const intervalMs = pollIntervalMs(process.env.CVBENCH_POLL_MS);
    do {
      await call(`/api/v1/operator/jobs${value ? `/${encodeURIComponent(value)}` : ""}`);
      if (value && terminal(lastBody?.job?.status)) break;
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    } while (true);
  } else if (["list", "detail", "audit", "evidence", "notes"].includes(command)) {
    const path = command === "list"
      ? `/api/v1/operator/jobs?limit=${encodeURIComponent(process.env.CVBENCH_OPERATOR_LIMIT || "25")}${value ? `&cursor=${encodeURIComponent(value)}` : ""}`
      : `/api/v1/operator/jobs/${encodeURIComponent(requiredArg(value, "job id"))}/${command === "detail" ? "" : command}`;
    await call(path.replace(/\/$/, ""));
  } else {
    throw new Error("Usage: cvbench-operator.mjs list [next-cursor] | watch [job-id] | detail <job-id> | audit <job-id> | evidence <job-id> | notes <job-id>");
  }

  async function call(path) {
    const response = await fetch(`${baseUrl}${path}`, { headers: { authorization: `Bearer ${token}`, accept: "application/json" } });
    const text = await response.text();
    try {
      lastBody = JSON.parse(text);
    } catch {
      throw new Error(`operator API returned non-JSON (${response.status})`);
    }
    if (!response.ok) throw new Error(`${response.status}: ${lastBody.error?.message || "operator API request failed"}`);
    process.stdout.write(`${JSON.stringify(lastBody, null, 2)}\n`);
  }
}

function terminal(status) {
  return status === "succeeded" || status === "failed";
}

function required(name) {
  const result = process.env[name];
  if (!result) throw new Error(`${name} is required`);
  return result;
}

function requiredArg(result, label) {
  if (!result) throw new Error(`${label} is required`);
  return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
