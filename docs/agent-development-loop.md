# Agent development loop

CVBench treats an agent as the primary model developer and a person as the
supervisor of each trusted run.

## The loop

1. A person gives an agent a model idea and points it at CVBench.
2. The agent writes the model, its runtime protocol, and a Dockerfile.
3. The agent runs `cvbench submit . --wait --json`.
4. The local CLI builds and uploads an immutable `linux/amd64` image.
5. The trusted runner reports live stages to the public result URL.
6. The terminal JSON gives the agent scores, prioritized findings, possible
   causes, and the next recommended benchmark test.
7. The agent changes one variable, increments the version, and repeats.

The person can keep the returned `result_url` open throughout the run. It polls
the same public record consumed by the CLI and shows queue state, runner stage,
published-scenario progress, source/model playback, metrics, and iteration
feedback.

## Model project

Only a Dockerfile is required. A small optional `cvbench.yaml` makes agent
intent explicit:

```yaml
schema_version: cvbench.agent/v1
name: woodland-tracker
version: iteration-1
dockerfile: Dockerfile
context: .
command: [python, -m, tracker]
```

If `command` is absent, the CLI combines the image's OCI `ENTRYPOINT` and
`CMD`. Source code, model weights, and dependencies stay inside the image.

## Commands

```bash
cvbench login
cvbench doctor
cvbench validate .
cvbench submit . --wait --json
cvbench status SUBMISSION_ID --json
```

`stdout` is reserved for the final JSON object when `--json` is used. Build,
upload, and progress messages go to `stderr`. A failed terminal benchmark exits
nonzero.

Submission idempotency is derived from the archive digest, image ID, command,
name, and version. Retrying an identical experiment cannot silently create a
second run.

## Image transport and trust

The CLI builds locally; CVBench does not execute source-level build steps. It
uploads a gzip-compressed Docker archive in fixed multipart chunks. The control
plane records the declared archive SHA-256, byte count, and Docker image ID.
The trusted runner downloads the private object, verifies every byte, imports
it, confirms the declared `linux/amd64` image identity, and only then starts the
existing network-disabled benchmark boundary.

Registry-digest submission remains supported for long-running projects and CI.
Both transports converge on the same runner, suite, isolation, scoring, and
public result record.

## Authentication

`cvbench login` reads a submission credential without echoing it. macOS stores
it in Keychain; Linux uses a mode-`0600` configuration file. Agents never need
the credential in their prompt or project directory.

CI agents may inject `CVBENCH_API_KEY` for one process instead. A future
identity provider can replace credential provisioning without changing the
artifact or submission protocol.
