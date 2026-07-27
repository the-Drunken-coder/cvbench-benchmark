import assert from "node:assert/strict";
import { test } from "node:test";

import { pollIntervalMs } from "../../scripts/cvbench-operator.mjs";

test("CLI poll interval rejects invalid and non-finite values", () => {
  for (const value of [undefined, "", " ", "not-a-number", "Infinity", "-Infinity"]) {
    assert.equal(pollIntervalMs(value), 5000);
  }
});

test("CLI poll interval keeps the valid one-second floor", () => {
  assert.equal(pollIntervalMs("-1"), 1000);
  assert.equal(pollIntervalMs("999"), 1000);
  assert.equal(pollIntervalMs("1000"), 1000);
  assert.equal(pollIntervalMs("2500"), 2500);
});
