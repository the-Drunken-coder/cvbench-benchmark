# Synthetic color tracker

This is the smallest complete CVBench reference system. It decodes each
progressively delivered JPEG, detects the synthetic green targets with OpenCV,
associates detections over time, coasts through short gaps, and emits the full
track lifecycle.

Canonical files:

- implementation: [`src/cvbench/examples/good_tracker.py`](../../../src/cvbench/examples/good_tracker.py)
- local system manifest: [`systems/example-good-local.yaml`](../../../systems/example-good-local.yaml)
- Docker system manifest: [`systems/example-good-docker.yaml`](../../../systems/example-good-docker.yaml)
- image definition: [`examples/Dockerfile.good`](../../Dockerfile.good)

Run locally:

```bash
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-local.yaml --output runs/
```

This tracker is intentionally specialized to the synthetic green-target
scenarios. It is protocol-complete, but it is not a competitive detector for
the person and vehicle classes in the real-video tranche.
