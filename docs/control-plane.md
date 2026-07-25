# Public control plane

CVBench's public control plane is one Cloudflare Worker with Static Assets and D1. It validates and queues immutable submitted system images; it never runs submitted code. A scheduled or manually dispatched GitHub-hosted Actions runner leases one job at a time and invokes the existing Docker-isolated CVBench engine.

Every public v1 submission runs the fixed `public-whole-system-tracking` Version 2 suite declared by `benchmarks/public-whole-system-v2.yaml`: 13 synthetic scenarios and 3 dense, full-frame real-video scenarios. The request schema remains compatible and has no benchmark selector. Queued/public records and runner leases state the exact assignment, and the Worker rejects a successful callback whose report identifies a different benchmark or version.

The same assignment is fixed to `cvbench.timing-compute/v1`, `cvbench.delivery-lossless/v1`, replay profile `native` at exactly 1.0x, and `cvbench.pareto/v1`. `/api/v1` accepts no replay-rate override, so another delivery pace cannot share the native leaderboard. Successful callbacks with a different timing, delivery, replay, or Pareto policy are rejected. Public and operator result summaries expose native duration, replay identity, CPU-seconds/native-source-second, real-time factor, peak RAM, and class while retaining the raw accuracy metrics.

```text
human or agent -> Worker API -> D1 queue
                       ^             |
                       | result      | lease
                       |             v
                 GitHub-hosted Linux runner -> Docker-isolated submitted image
```

The Worker source, site, migrations, and JavaScript tests live in `control-plane/`. The execution bridge is `scripts/run_control_plane_job.py`, and `.github/workflows/control-plane-runner.yml` schedules it.

## Security properties

- Submissions require `Authorization: Bearer ...` and accept only a prebuilt OCI image pinned by SHA-256, a bounded argv array, and bounded descriptive metadata.
- Source repositories, build steps, shell strings, environment variables, Docker socket access, and mutable image tags are rejected.
- Submission keys are compared through fixed-length SHA-256 digests with a constant-time byte comparison. D1 stores only the submitter-key digest.
- `Idempotency-Key` is unique per submitter-key digest. Repeating the same body returns the existing job; changing the body returns `409`.
- Public reads omit contact, notes, authentication data, lease data, raw submitted-system output, stderr, and raw evidence artifacts. They return score summaries and finding statements only.
- Operator reads use `OPERATOR_READ_API_KEYS`; adjudication writes use the secret JSON mapping `OPERATOR_ADJUDICATOR_CREDENTIALS={"actor/id":"token"}`. Each credential maps to exactly one stable actor identity; invalid, generic, duplicate, or cross-scope-overlapping credentials fail closed. Submission, runner, and read-only tokens cannot write notes. All bearer verification uses the same SHA-256 digest plus constant-time comparison path; only credential digests and the mapped actor ID are stored, never bearer values.
- Operator flags are deterministic review aids. They never automatically disqualify a submitted system; adjudication is an explicit note/verdict trail.
- A trusted runner bearer token protects leases and callbacks. Each lease also gets an independent random token, stored only as a digest, and state updates require `running -> succeeded|failed`. The 3000-second lease exceeds the 40-minute workflow timeout with callback margin.
- Each lease advertises the Worker's one-MiB result-body budget. The trusted runner preserves the complete scored report and deterministically retains head-and-tail stderr diagnostics that fit, recording original, retained, and omitted counts in the public result.
- Expired leases return to `queued` and can be attempted again. Old callback tokens stop working.
- The GitHub-hosted runner is ephemeral, has read-only repository permission, runs one job, and has no broad GitHub PAT in Cloudflare.
- Before invoking CVBench, the runner removes callback, Cloudflare, and GitHub secrets from the benchmark subprocess environment. The Docker adapter passes only `CVBENCH_INPUT_SOCKET` and explicitly submitted system configuration into the system-under-test image.
- The current execution envelope is one `linux/amd64` OCI image, network disabled, 4 CPUs, 2048 MB, one progressive socket-directory mount, no extra mounts, and no Docker socket. The adapter also enforces a host-aligned unprivileged UID/GID and exact image identity verification. Every submitted image gets a unique job label; both the runner and an `if: always()` workflow step force-remove and assert against survivors.
- Container/cgroup accounting charges all processes for CPU time, RAM, and I/O. Native source duration is immutable; startup, delivery, completion, drain, processing latency, backlog, deadline misses, and late output are reported separately. GPU data is omitted unless a device is genuinely isolated.

The one-image rule is a packaging, reproducibility, and security boundary. It is not a one-learned-model or one-process assumption. A system under test may combine a detector, tracker, temporal memory, association, filtering, and post-processing pipeline, including multiple cooperating processes, provided it connects to the progressive socket, emits `CVBENCH_READY`, and speaks `cvbench.track/v1`.

Public registry images are the zero-credential default. A manually operated runner may pre-authenticate Docker to a private registry, but registry credentials must never be added to submission metadata.

## Local, Docker-free Worker and site development

Node.js 20+ is required. Docker is not needed for the Worker, static site, D1 migration, or API lifecycle tests.

```bash
cd control-plane
npm ci
npm test
npx wrangler d1 migrations apply cvbench-control-plane --local
npx wrangler dev
```

Create `control-plane/.dev.vars` with local-only values (the file is ignored by Git):

```bash
SUBMISSION_API_KEYS="local-submission-key"
RUNNER_TOKEN="local-runner-token"
OPERATOR_READ_API_KEYS="local-operator-read-token"
OPERATOR_ADJUDICATOR_CREDENTIALS='{"local/alice":"local-alice-write-token","local/bob":"local-bob-write-token"}'
```

Then open the local URL printed by Wrangler. The health, contract, and OpenAPI endpoints are:

```bash
curl -sS http://localhost:8787/api/v1/health
curl -sS http://localhost:8787/api/v1/contract
curl -sS http://localhost:8787/api/v1/openapi.json
```

`npm run build` deterministically creates the allowlisted Static Assets tree in `control-plane/dist`, including the complete public scenario catalog. `npm test` exercises the catalog build plus a complete in-memory HTTP lifecycle: authenticated creation with the fixed 16-scenario assignment, idempotent replay, public read, lease, benchmark-bound scored result callback, terminal-state rejection, failure callback, rate limit, payload limit, and lease expiry. It uses a safe baseline system-image reference and a representative scored CVBench report; it does not execute Docker.

`npm ci` also invokes this build through the package's `postinstall` hook, and the build-time YAML parser is a production dependency so `npm ci --omit=dev` follows the same contract. An explicit `npm run build` remains useful only for local regeneration after source edits.

With `wrangler dev` running and a real scored baseline `report.json`, the same lifecycle can be checked against local D1:

```bash
set -a
. ./.dev.vars
set +a
CVBENCH_API_BASE_URL=http://127.0.0.1:8787 \
CVBENCH_API_KEY="$SUBMISSION_API_KEYS" \
CVBENCH_RUNNER_TOKEN="$RUNNER_TOKEN" \
CVBENCH_OPERATOR_READ_TOKEN="$OPERATOR_READ_API_KEYS" \
CVBENCH_OPERATOR_WRITE_TOKEN="local-alice-write-token" \
CVBENCH_OPERATOR_SECOND_WRITE_TOKEN="local-bob-write-token" \
CVBENCH_OPERATOR_ACTOR_ID="local/alice" \
CVBENCH_OPERATOR_SECOND_ACTOR_ID="local/bob" \
CVBENCH_REPORT_PATH=/absolute/path/to/report.json \
npm run test:d1
```

The Linux CI `docker-scored-e2e` builds the synthetic and real-video baseline images, prepares and hash-verifies the dense corpus, runs the complete public 16-scenario manifest through the Docker-isolated engine, asserts HOTA/IDF1 and the exact benchmark identity, and checks the tested containers are gone. Together with the Worker lifecycle tests, this covers public queue assignment through isolated execution and a benchmark-bound callback.

## Production with Cloudflare Workers Builds

Workers Builds Git integration is the deployment source of truth. Do not add a second GitHub deployment workflow.

In the Cloudflare dashboard:

1. Create or connect a Worker to `the-Drunken-coder/cvbench-system-runner`.
2. Set the root directory to `/control-plane`.
3. Set the build command to `npm ci`; `postinstall` performs the one deterministic catalog build.
4. Set the deploy command to `npx wrangler deploy`.
5. Set the production branch to `main`. Leave branch builds enabled so pull-request branches receive preview versions.
6. Allow Wrangler to provision the D1 binding named `DB` from `wrangler.jsonc`; the narrowly scoped database name is `cvbench-control-plane`.
7. After first provisioning, apply the schema from the same root:

   ```bash
   npx wrangler d1 migrations apply cvbench-control-plane --remote
   ```

8. Add encrypted Worker secrets. Generate independent high-entropy values and retain the runner value for the matching GitHub Actions secret:

   ```bash
   npx wrangler secret put SUBMISSION_API_KEYS
   npx wrangler secret put RUNNER_TOKEN
   npx wrangler secret put OPERATOR_READ_API_KEYS
   npx wrangler secret put OPERATOR_ADJUDICATOR_CREDENTIALS
   ```

   `SUBMISSION_API_KEYS` and `OPERATOR_READ_API_KEYS` accept comma-separated keys to allow rotation. All four credential scopes must contain globally unique bearer values; any overlap disables protected routes until corrected. `OPERATOR_ADJUDICATOR_CREDENTIALS` is a secret JSON actor-to-token mapping; rotate it as one secret and do not put bearer values in `wrangler.jsonc`, Actions variables, job metadata, PR text, or logs.

9. In GitHub repository settings, add the Actions variable `CVBENCH_API_BASE_URL` with the deployed `https://...workers.dev` origin. Create an environment named `cvbench-production`, restrict its deployment branches to `main` only, and put `CVBENCH_RUNNER_TOKEN` in that environment with exactly the same value as the Worker `RUNNER_TOKEN`. Do not keep a repository-level copy of this secret.
10. Manually dispatch **Trusted benchmark runner** once. The cron schedule checks for one queued job every 15 minutes.

Cloudflare account identifiers and API credentials remain dashboard/runtime configuration and are not committed.

## API lifecycle

Get the live contract before packaging a system:

```bash
curl -sS "$CVBENCH_API_BASE_URL/api/v1/contract"
```

Create a job using a digest returned by the registry, never a locally guessed digest:

```bash
curl -sS "$CVBENCH_API_BASE_URL/api/v1/submissions" \
  -H "Authorization: Bearer $CVBENCH_API_KEY" \
  -H "Idempotency-Key: tracker-v7-001" \
  -H "Content-Type: application/json" \
  --data '{
    "image":"ghcr.io/acme/tracker@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "argv":["python","-m","tracker"],
    "name":"Acme Temporal Tracker",
    "model_version":"7"
  }'
```

Poll the public `Location` returned by the create call. Status moves through `queued`, `running`, and one terminal state: `succeeded` with a score summary or `failed` with a bounded error. Complete report/evidence detail is operator-authenticated.

## Operator console and stable JSON API

The private dashboard is `/operator.html`. It polls JSON rather than scraping UI state. Keep the operator token in an environment variable or a local secret manager:

```bash
export CVBENCH_OPERATOR_READ_TOKEN='local-only-or-secret-manager-value'
curl -sS "$CVBENCH_API_BASE_URL/api/v1/operator/jobs?status=running" \
  -H "Authorization: Bearer $CVBENCH_OPERATOR_READ_TOKEN"

curl -sS "$CVBENCH_API_BASE_URL/api/v1/operator/jobs/$JOB_ID" \
  -H "Authorization: Bearer $CVBENCH_OPERATOR_READ_TOKEN"
curl -sS "$CVBENCH_API_BASE_URL/api/v1/operator/jobs/$JOB_ID/audit" \
  -H "Authorization: Bearer $CVBENCH_OPERATOR_READ_TOKEN"
curl -sS "$CVBENCH_API_BASE_URL/api/v1/operator/jobs/$JOB_ID/evidence" \
  -H "Authorization: Bearer $CVBENCH_OPERATOR_READ_TOKEN"
```

For a terminal-friendly watcher:

```bash
cd control-plane
CVBENCH_POLL_MS=5000 node ../scripts/cvbench-operator.mjs watch "$JOB_ID"
node ../scripts/cvbench-operator.mjs audit "$JOB_ID"
```

The operator job shape includes queue timestamps, lease expiry, attempts/retries, exact OCI digest, benchmark/scenario and comparison fingerprints, runner commit and workflow link, score components, failure reasons, and audit-flag counts. `/audit` explains denominator eligibility and positive credit and marks every anomaly as `review_aid_only`; `/evidence` returns bounded frame samples, matching decisions, observed/predicted/coasting counts, occlusion/reacquisition events, false-track segments, resource/isolation evidence, and reproducibility inputs. Raw JSONL/video artifacts are not uploaded or exposed by this public repository; evidence reports carry `sha256(cvbench.canonical-json/v1)` hashes computed authoritatively after Worker JSON parsing and `raw_evidence_available=false`.

Leave a fairness/adjudication trail without changing the score:

```bash
curl -sS -X POST "$CVBENCH_API_BASE_URL/api/v1/operator/jobs/$JOB_ID/notes" \
  -H "Authorization: Bearer $CVBENCH_OPERATOR_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"verdict":"accepted","note":"Reviewed sampled overlays and latency evidence; legitimate result."}'
```

Submitted-system output is untrusted data. The dashboard renders it with DOM text nodes, never `innerHTML`; the API returns JSON and does not turn system output into shell, HTML, or prompts. The operator threat analysis covers credential separation, hidden annotations and source paths, runner tokens, artifact-link expiry, prompt-injection-like output, duplicate fingerprints, exact-ground-truth replay, impossible timestamps, unread input, and isolation violations. A flag is evidence for a human review, not guilt.

The protected runner endpoints are deliberately omitted from the public OpenAPI operations. Their implementation and workflow are public, but their bearer and lease tokens are not part of the system-submission interface.

### Version 1 compatibility names

The Version 1 wire field `model_version`, response object `model`, internal property `modelVersion`, D1 column `model_version`, and operator query parameter `model` remain unchanged. They are compatibility names for the submitted system and its system version; they do not narrow the SUT to one learned component. This change deliberately does not rename the schema or design a Version 2 API.
