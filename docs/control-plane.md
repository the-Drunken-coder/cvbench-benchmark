# Public control plane

CVBench's public control plane is one Cloudflare Worker with Static Assets, D1,
and private R2 artifact storage. It validates and queues immutable submitted
system images; it never runs submitted code. A scheduled or manually
dispatched GitHub-hosted Actions runner leases one job at a time and invokes
the existing Docker-isolated CVBench engine.

Every new public v1 submission runs the fixed `public-whole-system-tracking` Version 3 suite declared by `benchmarks/public-whole-system-v3.yaml`: the same 13 synthetic scenarios and 3 dense, full-frame real-video scenarios, with an expanded 4-CPU, 8192-MiB, 240-second execution envelope. The request schema remains compatible and has no benchmark selector. Queued/public records and runner leases state the exact assignment, and the Worker rejects a successful callback whose report identifies a different benchmark or version. Completed historical records retain the benchmark identity, resource envelope, and run budgets stored in their report.

The same assignment is fixed to `cvbench.timing-compute/v1`, `cvbench.delivery-lossless/v1`, replay profile `native` at exactly 1.0x, and `cvbench.pareto/v1`. `/api/v1` accepts no replay-rate override, so another delivery pace cannot share the native leaderboard. Successful callbacks with a different timing, delivery, replay, or Pareto policy are rejected. Public and operator result summaries expose native duration, replay identity, CPU-seconds/native-source-second, real-time factor, peak RAM, and class while retaining the raw accuracy metrics.

```text
agent CLI -> private R2 upload -> Worker API -> D1 queue
                                        ^             |
human browser -> public result page     | result      | lease + progress
                                        |             v
                                  GitHub-hosted Linux runner
                                             |
                                             v
                                  Docker-isolated submitted image
```

The Worker source, site, migrations, and JavaScript tests live in `control-plane/`. The execution bridge is `scripts/run_control_plane_job.py`, and `.github/workflows/control-plane-runner.yml` schedules it.

## Public control pane

The public UI is static HTML, CSS, and one small browser module in the existing
Worker asset pipeline. It has four clear destinations:

- `/datasets/` is a table-first, read-only view generated from an exact
  `cvbench-dataset` commit;
- `/runs/` opens the dedicated public result page for one submission UUID;
- `/submit/` leads with `cvbench submit . --wait --json` and keeps registry
  submission behind a disclosure;
- `/docs/` contains the compact runtime contract and links to the exact JSON,
  OpenAPI, scenarios, and repositories.

`/scenarios/` and `/results/` remain focused viewers. `/operator.html` remains
private and is not linked from the public application shell.

Refresh the checked-in dataset projection from a clean local Dataset checkout
that matches `control-plane/dataset-catalog-source.lock.json`. The lock binds
the merged revision, every dataset/example input byte, and the complete
generated catalog projection with SHA-256:

```bash
cd control-plane
npm run sync:datasets -- /path/to/cvbench-dataset
```

The sync selects only public package, clip, source, license, media, model, and
review-count metadata into `/dataset-catalog/v1/catalog.json`. Small browser
previews are bound to their exact content hash and published for dataset
inspection. Preview clips also carry a bounded projection of sparse boxes with
source timestamp, rectangle, track identity, class, and confidence so the
browser can toggle model proposals during playback. Original media, reviewer
identities, local paths, and mutable Studio authoring state are not copied into
the hosted catalog.

Preview files use silent H.264 at 854 pixels wide, 30 FPS, CRF 30, `yuv420p`,
and fast-start metadata. Their filenames include the dataset and clip IDs plus
the first 12 characters of both the source-media SHA-256 and preview SHA-256.
The sync command records the full source hash, preview hash, and byte count, and
the build derives its exact asset allowlist from those validated declarations.

## Security properties

- Artifact creation and submission require `Authorization: Bearer ...`.
  CVBench accepts either a prebuilt OCI image uploaded directly by the agent
  CLI with an archive hash, exact Docker image ID, and byte count, or a
  registry image pinned by SHA-256. Both transports accept only a bounded argv
  array and bounded descriptive metadata.
- Source repositories, build steps, shell strings, environment variables, Docker socket access, and mutable image tags are rejected.
- Submission keys are compared through fixed-length SHA-256 digests with a constant-time byte comparison. D1 stores only the submitter-key digest.
- `Idempotency-Key` is unique per submitter-key digest. Repeating the same body returns the existing job; changing the body returns `409`.
- Multipart part numbers are reserved atomically before R2 receives bytes.
  Completed identical retries replay the recorded part response; conflicting or
  in-flight duplicates cannot replace the ETag used at completion.
- Public reads omit contact, notes, authentication data, lease data, raw submitted-system output, stderr, and raw evidence artifacts. They return score summaries, finding statements, and (for new successful runs) a bounded visualization projection of submitted tracks.
- Prediction playback aliases model-controlled track IDs and retains only frame-relative geometry, a small public class vocabulary, state/support/event enums, and confidence. It excludes arbitrary output fields, matching decisions, ground truth, diagnostics, paths, commands, and collector timestamps. Historical runs honestly report playback as unavailable.
- Operator reads use `OPERATOR_READ_API_KEYS`; adjudication writes use the secret JSON mapping `OPERATOR_ADJUDICATOR_CREDENTIALS={"actor/id":"token"}`. Each credential maps to exactly one stable actor identity; invalid, generic, duplicate, or cross-scope-overlapping credentials fail closed. Submission, runner, and read-only tokens cannot write notes. All bearer verification uses the same SHA-256 digest plus constant-time comparison path; only credential digests and the mapped actor ID are stored, never bearer values.
- Operator flags are deterministic review aids. They never automatically disqualify a submitted system; adjudication is an explicit note/verdict trail.
- A trusted runner bearer token protects leases and callbacks. Each lease also gets an independent random token, stored only as a digest, and state updates require `running -> succeeded|failed`. The 3000-second lease exceeds the 40-minute workflow timeout with callback margin.
- Each lease advertises the Worker's one-MiB result-body budget. The trusted runner preserves the complete scored report and deterministically retains head-and-tail stderr diagnostics that fit, recording original, retained, and omitted counts in the public result.
- Expired leases return to `queued` and can be attempted again. Old callback tokens stop working.
- The runner verifies the compressed archive before use, then streams gzip
  expansion into `docker load` through an independent eight-GiB expanded-byte
  ceiling. It kills the loader on overflow or timeout instead of materializing
  an unbounded archive on runner storage.
- The GitHub-hosted runner is ephemeral, has read-only repository permission, runs one job, and has no broad GitHub PAT in Cloudflare.
- Cloudflare branch versions inherit the Worker's production bindings. The
  `PRODUCTION_HOSTNAME` gate therefore makes every non-production hostname
  read-only before authentication or storage access: public health, contract,
  OpenAPI, and result reads remain testable, while submissions, artifacts,
  runner routes, and operator routes return `preview_read_only`.
- Before invoking CVBench, the runner removes callback, Cloudflare, and GitHub secrets from the benchmark subprocess environment. The Docker adapter passes only `CVBENCH_INPUT_SOCKET` and explicitly submitted system configuration into the system-under-test image.
- The current execution envelope is one `linux/amd64` OCI image, network disabled, 4 CPUs, 8192 MB total memory with swap disabled, a 512-process ceiling, one progressive socket-directory mount, no extra mounts, and no Docker socket. The adapter also enforces a host-aligned unprivileged UID/GID and exact image identity verification. Every submitted image gets a unique job label; both the runner and an `if: always()` workflow step force-remove and assert against survivors.
- Container/cgroup accounting charges all processes for CPU time, RAM, and I/O. Native source duration is immutable; startup, delivery, completion, drain, processing latency, backlog, deadline misses, and late output are reported separately. GPU data is omitted unless a device is genuinely isolated.

The one-image rule is a packaging, reproducibility, and security boundary. It is not a one-learned-model or one-process assumption. A system under test may combine a detector, tracker, temporal memory, association, filtering, and post-processing pipeline, including multiple cooperating processes, provided it connects to the progressive socket, emits `CVBENCH_READY`, and speaks `cvbench.track/v1`.

Direct private upload is the default agent path and removes registry accounts,
tags, pushes, and digest lookup from the iteration loop. Registry-digest
submission remains supported for established CI systems. A manually operated
runner may pre-authenticate Docker to a private registry, but registry
credentials must never be added to submission metadata.

## Local, Docker-free Worker and site development

Node.js 20+ is required. Docker is not needed for the Worker, static site, D1 migration, or API lifecycle tests.

```bash
cd control-plane
npm ci
npm test
npx wrangler d1 migrations apply cvbench-control-plane --local
npm run dev
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

`npm run build` deterministically creates the allowlisted Static Assets tree in
`control-plane/dist`, including the dataset and complete public scenario
catalogs. `npm run dev` always performs that build before starting Wrangler, so
the Worker never serves a stale UI. `npm test`
exercises the UI and catalog build plus a complete in-memory HTTP lifecycle:
authenticated creation with the fixed 16-scenario assignment, idempotent
replay, public read, lease, benchmark-bound scored result callback,
terminal-state rejection, failure callback, rate limit, payload limit, and
lease expiry. It uses a safe baseline system-image reference and a
representative scored CVBench report; it does not execute Docker.

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

1. Create or connect a Worker to `the-Drunken-coder/cvbench-benchmark`.
2. Set the root directory to `/control-plane`.
3. Set the build command to `npm ci`; `postinstall` performs the one deterministic catalog build.
4. Set the deploy command to `npx wrangler deploy`.
5. Set the production branch to `main`. Branch builds may remain enabled for
   read-only UI previews; the committed `PRODUCTION_HOSTNAME` gate prevents
   those version URLs from reaching authenticated, runner, operator, or
   mutating routes even though Cloudflare versions inherit production
   bindings.
6. Keep the explicit production `database_id` in `wrangler.jsonc`.
   `preview_database_id` and `preview_bucket_name` isolate `wrangler dev
   --remote` sessions only; they do not isolate Workers Builds versions. Apply
   all migrations to the empty development-preview database with
   `npx wrangler d1 migrations apply DB --remote --preview` before using remote
   development.
7. Create the private R2 bucket declared by the `SUBMISSION_ARTIFACTS` binding:

   ```bash
   npx wrangler r2 bucket create cvbench-submission-artifacts
   npx wrangler r2 bucket create cvbench-submission-artifacts-preview
   ```

   Production archives expire after 90 days; preview archives expire after
   seven days. The result records and public playback projections are stored
   separately and are not removed by these rules:

   ```bash
   npx wrangler r2 bucket lifecycle add cvbench-submission-artifacts \
     cvbench-agent-images-retention agent-images/ --expire-days 90
   npx wrangler r2 bucket lifecycle add cvbench-submission-artifacts-preview \
     cvbench-preview-agent-images-retention agent-images/ --expire-days 7
   ```

   Submitted archives are private and are exposed only to an authenticated
   trusted runner.
8. After first provisioning, apply the schema from the same root:

   ```bash
   npx wrangler d1 migrations apply cvbench-control-plane --remote
   ```

9. Add encrypted Worker secrets. Generate independent high-entropy values and retain the runner value for the matching GitHub Actions secret:

   ```bash
   npx wrangler secret put SUBMISSION_API_KEYS
   npx wrangler secret put RUNNER_TOKEN
   npx wrangler secret put OPERATOR_READ_API_KEYS
   npx wrangler secret put OPERATOR_ADJUDICATOR_CREDENTIALS
   ```

   `SUBMISSION_API_KEYS` and `OPERATOR_READ_API_KEYS` accept comma-separated keys to allow rotation. All four credential scopes must contain globally unique bearer values; any overlap disables protected routes until corrected. `OPERATOR_ADJUDICATOR_CREDENTIALS` is a secret JSON actor-to-token mapping; rotate it as one secret and do not put bearer values in `wrangler.jsonc`, Actions variables, job metadata, PR text, or logs.

10. In GitHub repository settings, add the Actions variable `CVBENCH_API_BASE_URL` with the deployed `https://...workers.dev` origin. Create an environment named `cvbench-production`, restrict its deployment branches to `main` only, and put `CVBENCH_RUNNER_TOKEN` in that environment with exactly the same value as the Worker `RUNNER_TOKEN`. Do not keep a repository-level copy of this secret.
11. Manually dispatch **Trusted benchmark runner** once. The cron schedule checks for one queued job every 15 minutes.

Cloudflare account identifiers and API credentials remain dashboard/runtime configuration and are not committed.

## API lifecycle

Get the live contract before packaging a system:

```bash
curl -sS "$CVBENCH_API_BASE_URL/api/v1/contract"
```

The primary agent path is:

```bash
cvbench login
cvbench doctor
cvbench submit . --wait --json
```

This builds `linux/amd64`, multipart-uploads a gzip-compressed Docker archive,
queues the run, prints stage updates to stderr, and returns a stable
`cvbench.agent-result/v1` JSON object containing the result URL, raw scores,
prioritized findings, likely causes, and a recommended next test. See
[Agent development loop](agent-development-loop.md).

Established CI systems can still create a job using a digest returned by their
registry, never a locally guessed digest:

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
