# Example model catalog

CVBench scores complete online tracking systems, not a model file in isolation.
Each reference includes detection, temporal association, protocol handling, and
lifecycle output.

| Reference | Approach | Runs now | Intended use |
|---|---|---:|---|
| [Synthetic color tracker](synthetic-color-tracker/README.md) | OpenCV color segmentation and constant-velocity tracking | Yes, local or Docker | Smallest complete integration example |
| [Real-video motion baseline](real-video-motion-baseline/README.md) | OpenCV frame differencing and nearest-centre tracking | Yes, local or Docker | Transparent model-free lower bound |
| [CVBench Lite MOT](lite-mot/README.md) | YOLOX-Nano plus ByteTrack-style association | Docker build | Small learned CPU baseline |
| [CVBench Balanced MOT](balanced-mot/README.md) | YOLOX-Tiny plus observation-centric association | Docker build | Better continuity and identity quality |
| [CVBench Advanced MOT](advanced-mot/README.md) | YOLOX-L plus optical-flow propagation | Docker build | Maximum quality inside the public envelope |
| [Learned-model packaging guide](learned-model-template/README.md) | Bring a detector or segmenter and temporal tracker | Bring weights | Starting point for another runtime |

All three systems map COCO `person`, `car`, `motorcycle`, `bus`, `truck`, and `dog`
outputs into the public CVBench ontology. A deterministic pixel-only fallback
handles the small green `synthetic_target` class. Unsupported COCO classes are
discarded.

The submitted process sees only progressively delivered pixels and its own
tracker state. It never receives scenario identifiers, labels, ground truth,
or future frames.

## Build and run

```bash
docker build -f examples/Dockerfile.lite-mot -t cvbench-example-lite-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/example-lite-mot-docker.yaml --output runs/

docker build -f examples/Dockerfile.balanced-mot -t cvbench-example-balanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/example-balanced-mot-docker.yaml --output runs/

docker build -f examples/Dockerfile.advanced-mot -t cvbench-example-advanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/example-advanced-mot-docker.yaml --output runs/
```

The model graph is downloaded only while building the image, with an exact
SHA-256 enforced by Docker. The resulting container includes the graph and
runs with network access disabled.

The older `good_tracker.py` color tracker and `real_video_baseline.py` motion
tracker remain useful as tiny integration fixtures. They are not recommended
as representative submissions.

## Repository policy for model weights

Do not commit checkpoints, exported graphs, registry credentials, or private
training data. Keep source, locked runtime dependencies, model notices, and
reproducible image instructions. Put weights inside the final OCI image and
submit it by immutable registry digest.

Every public image must be `linux/amd64`, run without network access, stay
inside the resources declared by the benchmark contract, connect to
`CVBENCH_INPUT_SOCKET`, print `CVBENCH_READY`, and emit one
`cvbench.track/v1` object per stdout line. Diagnostics belong on stderr.
