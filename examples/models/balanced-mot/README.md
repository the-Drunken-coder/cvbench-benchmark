# CVBench Balanced MOT

Balanced spends more CPU and memory for a stronger detector and more persistent
identity association:

- official COCO-pretrained YOLOX-Tiny at 416 pixels
- deterministic moving-object filter for static-camera video
- observation-centric direction and IoU association
- constant-velocity coasting and reacquisition
- pixel-only synthetic target fallback

Build and run:

```bash
docker build -f examples/Dockerfile.balanced-mot -t cvbench-example-balanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v2.yaml \
  --system systems/example-balanced-mot-docker.yaml --output runs/
```

Canonical files:

- adapter: `src/cvbench/examples/reference_mot.py`
- entrypoint: `src/cvbench/examples/balanced_mot.py`
- image: `examples/Dockerfile.balanced-mot`
- system manifest: `systems/example-balanced-mot-docker.yaml`

The association code uses OC-SORT's observation-centric design idea without
vendoring or claiming exact equivalence to the official OC-SORT implementation.
