# CVBench Lite MOT

Lite is the default learned reference for a modest CPU budget:

- official COCO-pretrained YOLOX-Nano at 416 pixels
- deterministic moving-object filter for static-camera video
- ByteTrack-style high/low confidence association
- constant-velocity coasting and reacquisition
- pixel-only synthetic target fallback

Build and run:

```bash
docker build -f examples/Dockerfile.lite-mot -t cvbench-example-lite-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/example-lite-mot-docker.yaml --output runs/
```

Canonical files:

- adapter: `src/cvbench/examples/reference_mot.py`
- entrypoint: `src/cvbench/examples/lite_mot.py`
- image: `examples/Dockerfile.lite-mot`
- system manifest: `systems/example-lite-mot-docker.yaml`

The association code is intentionally compact and inspired by ByteTrack's
two-stage use of high- and low-confidence detections; it is not a vendored copy
of the official ByteTrack implementation.
