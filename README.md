# CVBench Benchmark

CVBench is a local-first black-box benchmark for complete online computer-vision tracking systems. It progressively sends timestamped JPEG frames over a Unix-domain socket, captures live JSONL track events with an external monotonic timestamp, deterministically matches them to ground truth, and writes separate accuracy, robustness, latency, resource, and diagnostic results.

This repository owns the benchmark runner, scorer, reports, trusted execution
worker, public control plane, and versioned execution contracts. Dataset
authoring and certification live in the separate dataset repository; model
experiments can use the agent-first submission CLI in this repository.

## Agent development loop

An agent needs a model directory with a `Dockerfile`, then one command:

```bash
cvbench submit . --wait --json
```

The CLI builds `linux/amd64`, infers `ENTRYPOINT` + `CMD`, creates a
gzip-compressed immutable Docker archive, uploads it directly to private
benchmark storage, queues the trusted run, follows progress, and returns a
stable `cvbench.agent-result/v1` object. No public registry is required.

One human setup action stores the submission credential outside the project:

```bash
cvbench login
cvbench doctor
```

On macOS the credential is stored in Keychain. On Linux it is stored in an
owner-only configuration file. `CVBENCH_API_KEY` remains available for
ephemeral CI agents. See [the agent development-loop guide](docs/agent-development-loop.md).

Native camera timestamps, delivery pace, and system completion are separate clocks. The versioned [timing and compute contract](docs/timing-compute-contract.md) reports cgroup CPU-seconds per native source-second and real-time factor as first-class Pareto axes; slower replay never rewrites source FPS or shares the native leaderboard class.

Version 1 is a Python modular monolith. The execution adapters are replaceable; scoring does not import Docker, subprocess, filesystem, or report-rendering code.

## Quick start

Python 3.11+ is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cvbench scenarios generate scenarios/synthetic-v1
cvbench validate --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-local.yaml
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-local.yaml --output runs/
```

The committed synthetic pack is already generated, so regeneration is only needed to prove determinism. The run directory contains `report.json`, `report.html`, externally timestamped `system-output.jsonl`, run-epoch `ground-truth.jsonl` with immutable native-relative timestamps retained, `resources.csv`, and evidence packets for major findings.

## Docker SUT

Scored Docker execution requires a Linux Docker host. Docker Desktop on macOS verifies the container boundary but cannot carry a host Unix socket through its VM bind mount; use the local adapter on macOS.

```bash
docker build -f examples/Dockerfile.good -t cvbench-example-good:v1 .
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-docker.yaml --output runs/
```

The runner mounts only a temporary socket directory, disables container networking, applies declared CPU and memory limits, resolves and executes the image by immutable digest or ID, verifies the running container reports that exact image identity, and reads CPU, RAM, process, and disk-I/O accounting externally from the host cgroup v2 hierarchy. It never executes an accounting command inside the submitted image, so distroless and `scratch` systems are supported. The SUT cannot inspect future scenario frames.

## Commands

- `cvbench run --benchmark ... --system ... --output ...` executes and reports a run.
- `cvbench validate .` builds and verifies an agent model project for `linux/amd64`.
- `cvbench submit . --wait --json` uploads, runs, and returns machine-readable iteration feedback.
- `cvbench status SUBMISSION_ID --json` returns the same live record without authentication.
- `cvbench validate --benchmark ... --system ...` validates benchmark configs, scenarios, frames, and ground truth.
- `cvbench scenarios generate PATH` regenerates the CC0 synthetic Version 1 pack.

See [Architecture](docs/architecture.md), [Protocol](docs/protocol.md), [Metrics](docs/metrics.md), [Development](docs/development.md), the [Version 1 capability matrix](docs/capability-matrix.md), and the [verbatim implementation specification](PROJECT_SPEC_VERBATIM.md).

Runnable learned reference systems, model notices, and build commands live in
the [example model catalog](examples/models/README.md).

New datasets are consumed only as hash-pinned immutable releases through the
[dataset boundary](datasets/README.md). The inline
[`real-video-v2`](docs/real-video-sources.md) assets are a frozen compatibility
fixture retained solely to reproduce existing benchmark results.

The separate [recovered clean-video training corpus](docs/recovered-training-corpus.md) converts five operator-supplied
Pixabay/Pexels originals into a local pseudo-labeled object-detection dataset. Its clean source videos, full-resolution
training images, and labels remain under ignored `.local-ingest/` storage, are explicitly evaluation-ineligible, and cannot
be loaded as benchmark scenarios. Five transformed, annotated browser previews are published at `/training/` with a
separate training-only catalog at `/training-media/v1/catalog.json`.

## Public scenario catalog and control plane

Every scenario referenced by the current benchmark manifests is published at `/scenarios/`: 13 synthetic scenarios and 3 real-video scenarios, with exact benchmark JPEGs, full public annotations, scoring boundaries, provenance, licenses, hashes, and allowlisted first-party baseline summaries. The stable discovery endpoint is `/.well-known/cvbench-scenarios.json`; see [the catalog architecture and build contract](docs/scenario-catalog.md).

The public submission queue is one Cloudflare Worker with Static Assets, D1,
and private R2 artifact storage. Every new v1 submission is assigned the fixed
`public-whole-system-tracking` Version 3 suite: the same 13 deterministic
synthetic scenarios and 3 dense real-video scenarios, with 4 CPUs, 8 GiB RAM,
no network, and a 240-second run budget. The assigned suite is present in
queued records, runner leases, public results, the contract, and OpenAPI;
callbacks for a different suite are rejected. Historical completed records
retain the benchmark version and execution envelope they actually used.
Untrusted submitted-system code never runs in Cloudflare: a scheduled or
manually dispatched ephemeral GitHub-hosted Linux runner leases one immutable
OCI image—either uploaded directly by the agent CLI or pinned by registry
digest—and executes it through the existing Docker-isolated engine.

See the [control-plane architecture, local commands, API lifecycle, security boundary, and Workers Builds setup](docs/control-plane.md). The [exact control-plane implementation input](docs/CONTROL_PLANE_IMPLEMENTATION_PROMPT.md) and [exact scenario-catalog implementation input](docs/SCENARIO_CATALOG_IMPLEMENTATION_PROMPT.md) are preserved alongside the original product specification.
