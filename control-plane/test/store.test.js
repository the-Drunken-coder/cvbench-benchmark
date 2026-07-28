import assert from "node:assert/strict";
import test from "node:test";

import { D1Store } from "../src/store.js";

test("D1 terminal success keeps the public succeeded status", async () => {
  let bindings = null;
  const db = {
    prepare(sql) {
      assert.match(sql, /UPDATE submissions SET status/);
      return {
        bind(...values) {
          bindings = values;
          return {
            async run() {
              return { meta: { changes: 0 } };
            },
          };
        },
      };
    },
  };
  const store = new D1Store(db);
  const completed = await store.completeJob({
    id: "submission-id",
    leaseTokenHash: "lease-hash",
    status: "succeeded",
    report: {},
    resultSha256: "result-hash",
    error: null,
    now: 123,
  });

  assert.equal(completed, null);
  assert.equal(bindings[0], "succeeded");
  assert.equal(bindings[8], "completed");
  assert.equal(bindings[9], "Benchmark completed.");
});
