# Example systems

Start with the [example model catalog](models/README.md). Its Lite and Balanced
systems are learned, class-aware references for the complete public suite. The
older color and frame-difference systems remain intentionally small protocol
fixtures and lower bounds.

`example-good-local.yaml` runs a deterministic classical OpenCV tracker. It decodes every delivered JPEG, segments the synthetic green targets, performs nearest-neighbor stateful association, emits tentative/confirmed/reacquired observations, and emits predicted/coasting updates during short gaps.

`example-broken-local.yaml` is intentionally bad while remaining schema-valid: it emits a new ID and a fixed false box on every frame. It exists to exercise diagnostics and regression reporting.

Build the container example from the repository root:

```bash
docker build -f examples/Dockerfile.good -t cvbench-example-good:v1 .
cvbench run --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-good-docker.yaml --output runs/
```

The runner resolves and records the local image ID or repository digest before a scored Docker run. The container receives only the socket directory, has networking disabled, and never receives the source-video directory.

Build the learned references from the repository root:

```bash
docker build -f examples/Dockerfile.lite-mot -t cvbench-example-lite-mot:v1 .
docker build -f examples/Dockerfile.balanced-mot -t cvbench-example-balanced-mot:v1 .
docker build -f examples/Dockerfile.advanced-mot -t cvbench-example-advanced-mot:v1 .
```

Each build downloads one official YOLOX ONNX checkpoint and verifies its
declared SHA-256 before placing it in the image. Runtime inference remains
fully offline.
