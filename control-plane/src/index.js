import { createApp } from "./app.js";
import { D1Store } from "./store.js";

export default {
  fetch(request, env) {
    const app = createApp({
      store: new D1Store(env.DB),
      assets: env.ASSETS,
      submissionKeys: env.SUBMISSION_API_KEYS || "",
      runnerToken: env.RUNNER_TOKEN || "",
      operatorReadKeys: env.OPERATOR_READ_API_KEYS || env.OPERATOR_API_TOKEN || "",
      operatorAdjudicatorCredentials: env.OPERATOR_ADJUDICATOR_CREDENTIALS || "",
      maxSubmissionsPerHour: env.MAX_SUBMISSIONS_PER_HOUR,
      leaseSeconds: env.LEASE_SECONDS,
    });
    return app.fetch(request);
  },
};
