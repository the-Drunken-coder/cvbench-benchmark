# Example model catalog

CVBench scores complete online tracking systems, so each learned reference
includes detection, association, lifecycle output, and the frame-socket
adapter.

| Reference | Detector | Association | Best for |
|---|---|---|---|
| [CVBench Lite MOT](lite-mot/README.md) | YOLOX-Nano, COCO | Two-pass ByteTrack-style IoU and motion | Small image and fast CPU baseline |
| [CVBench Balanced MOT](balanced-mot/README.md) | YOLOX-Tiny, COCO | Observation-centric IoU and motion | Better continuity and identity quality |
| [CVBench Advanced MOT](advanced-mot/README.md) | YOLOX-L, COCO | Optical-flow propagation and two-pass observation-centric association | Maximum quality inside the public runner envelope |

Both systems map COCO `person`, `car`, `motorcycle`, `bus`, `truck`, and `dog`
outputs into the public CVBench ontology. A deterministic pixel-only fallback
handles the small green `synthetic_target` class. Unsupported COCO classes are
discarded.

The submitted process sees only progressively delivered pixels and its own
tracker state. It never receives scenario identifiers, labels, ground truth,
or future frames.

## Build and run

```bash
docker build -f examples/Dockerfile.lite-mot -t cvbench-example-lite-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v2.yaml \
  --system systems/example-lite-mot-docker.yaml --output runs/

docker build -f examples/Dockerfile.balanced-mot -t cvbench-example-balanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v2.yaml \
  --system systems/example-balanced-mot-docker.yaml --output runs/

docker build -f examples/Dockerfile.advanced-mot -t cvbench-example-advanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v2.yaml \
  --system systems/example-advanced-mot-docker.yaml --output runs/
```

The model graph is downloaded only while building the image, with an exact
SHA-256 enforced by Docker. The resulting container includes the graph and
runs with network access disabled.

The older `good_tracker.py` color tracker and `real_video_baseline.py` motion
tracker remain useful as tiny integration fixtures. They are not recommended
as representative submissions.
