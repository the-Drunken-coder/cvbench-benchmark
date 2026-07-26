# CVBench Advanced MOT

Advanced prioritizes tracking quality within the public runner's fixed
4-CPU, 2-GiB container envelope:

- official COCO-pretrained YOLOX-L at its native 640-pixel input
- deterministic moving-object filtering for static-camera video
- 640-pixel detector refreshes with dense optical-flow propagation between them
- two-stage high/low-confidence association
- observation-centric direction and IoU matching
- constant-velocity coasting and long-lived same-ID reactivation
- pixel-only synthetic target fallback

Build and run:

```bash
docker build -f examples/Dockerfile.advanced-mot -t cvbench-example-advanced-mot:v1 .
cvbench run --benchmark benchmarks/public-whole-system-v2.yaml \
  --system systems/example-advanced-mot-docker.yaml --output runs/
```

Canonical files:

- adapter: `src/cvbench/examples/reference_mot.py`
- entrypoint: `src/cvbench/examples/advanced_mot.py`
- image: `examples/Dockerfile.advanced-mot`
- system manifest: `systems/example-advanced-mot-docker.yaml`

YOLOX-X was also measured during development, but exceeded both the 2-GiB
memory cap and the 90-second suite deadline. YOLOX-L is the largest official
YOLOX model that satisfies the public runner envelope.

The association code uses ByteTrack and OC-SORT design ideas without vendoring
or claiming exact equivalence to either official implementation.
