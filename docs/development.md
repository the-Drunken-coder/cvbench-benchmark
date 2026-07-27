# Development and independent validation

## Fresh environment

```bash
python3 -m venv /tmp/cvbench-review-venv
source /tmp/cvbench-review-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cvbench validate --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-local.yaml
pytest
ruff check .
```

## End-to-end evidence

```bash
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-local.yaml --output /tmp/cvbench-review-runs
for report in /tmp/cvbench-review-runs/*/report.json; do
  python -m json.tool "$report" >/dev/null
done
```

The good system must decode and score real JPEG frames. Run the broken system to verify failure detection:

```bash
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-broken-local.yaml --output /tmp/cvbench-broken-runs
```

## Docker evidence

If a Docker daemon is available:

```bash
docker build -f examples/Dockerfile.good -t cvbench-example-good:v1 .
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-docker.yaml --output /tmp/cvbench-docker-runs
python scripts/assert_docker_report.py /tmp/cvbench-docker-runs
```

The CI workflow covers install, lint, tests, scenario validation, and a real scored Linux Docker run. The Docker job builds the good image and asserts owner-only socket permissions, aligned non-root UID/GID execution, socket-only mounting, disabled networking, applied CPU/RAM limits, resource samples, an immutable image digest, scored matches, and complete container cleanup. Hardware accelerators are not required. NVIDIA collection degrades explicitly to unavailable.

It also runs `benchmarks/timing-compute-evidence.yaml` against fast, CPU-heavy, idle, background-child, final-burst, and immediate-clean-exit variants of the same vision system. The immediate-exit harness deliberately holds the final stdout record in the collector until the half-close boundary is requested, then proves that the record is scored, a fresh retained-parent-cgroup sample is authoritative, and both the container and parent scope are removed. The compact evidence is checked by `scripts/assert_pacing_evidence.py` and uploaded as `timing-compute-docker-evidence`.
