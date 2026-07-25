# CVBench System Runner

CVBench is a local-first black-box benchmark for complete online computer-vision tracking systems. It progressively sends timestamped JPEG frames over a Unix-domain socket, captures live JSONL track events with an external monotonic timestamp, deterministically matches them to ground truth, and writes separate accuracy, robustness, latency, resource, and diagnostic results.

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
- `cvbench validate --benchmark ... --system ...` validates configs, scenarios, frames, and ground truth.
- `cvbench scenarios generate PATH` regenerates the CC0 synthetic Version 1 pack.

See [Architecture](docs/architecture.md), [Protocol](docs/protocol.md), [Metrics](docs/metrics.md), [Development](docs/development.md), the [Version 1 capability matrix](docs/capability-matrix.md), and the [verbatim implementation specification](PROJECT_SPEC_VERBATIM.md).

The dense real-video-v2 corpus is documented in [Real-video sources and annotation audit](docs/real-video-sources.md). It contains three native-30-FPS, full-frame clips derived from the CC BY 4.0 MEVA dataset and its upstream per-frame tracking labels. Preparation verifies pinned source and annotation hashes; no unlabeled ordinary video or unreviewed interpolation is benchmark truth.

The conditional noncommercial MOTChallenge tranche is documented in [MOTChallenge dense pedestrian tranche](docs/motchallenge-sources.md). It adds exactly ten known-public pedestrian sequences with official-archive hashes, derived ordinal-JPEG cadence disclosure, a separate CC BY-NC-SA 3.0 asset boundary, deterministic hydration, neutral evaluator-only ignores, and no interpolated truth.

## Public scenario catalog and control plane

Every scenario referenced by the current benchmark manifests is published at `/scenarios/`: 13 synthetic scenarios, 3 MEVA real-video scenarios, and 10 MOTChallenge pedestrian scenarios, with exact frame identities, full public annotations, scoring boundaries, provenance, licenses, hashes, viewer media, and allowlisted first-party baseline summaries. The stable discovery endpoint is `/.well-known/cvbench-scenarios.json`; see [the catalog architecture and build contract](docs/scenario-catalog.md).

The public submission queue is one Cloudflare Worker with Static Assets and D1. Every v1 submission is assigned the fixed `public-whole-system-tracking` Version 3 suite: all 13 deterministic synthetic scenarios, all 3 MEVA real-video scenarios, and exactly the 10 selected MOTChallenge scenarios. The assigned suite is present in queued records, runner leases, public results, the contract, and OpenAPI; callbacks for a different suite are rejected. Untrusted submitted-system code never runs in Cloudflare: a scheduled or manually dispatched ephemeral GitHub-hosted Linux runner leases one digest-pinned OCI image and executes it through the existing Docker-isolated engine.

See the [control-plane architecture, local commands, API lifecycle, security boundary, and Workers Builds setup](docs/control-plane.md). The [exact control-plane implementation input](docs/CONTROL_PLANE_IMPLEMENTATION_PROMPT.md) and [exact scenario-catalog implementation input](docs/SCENARIO_CATALOG_IMPLEMENTATION_PROMPT.md) are preserved alongside the original product specification.
