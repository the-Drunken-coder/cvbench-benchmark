# Real-video motion baseline

This weight-free baseline uses adjacent-frame differencing, simple shape-based
class assignment, and constant-velocity association. It receives only the
current frame and its retained tracker state; it has no scenario map, target
query, labels, or ground-truth access.

Canonical files:

- implementation: [`src/cvbench/examples/real_video_baseline.py`](../../../src/cvbench/examples/real_video_baseline.py)
- local system manifest: [`systems/real-video-baseline-local.yaml`](../../../systems/real-video-baseline-local.yaml)
- Docker system manifest: [`systems/real-video-baseline-docker.yaml`](../../../systems/real-video-baseline-docker.yaml)
- image definition: [`examples/Dockerfile.real-video-baseline`](../../Dockerfile.real-video-baseline)

Hydrate the committed real-video frame archives, then run the local adapter:

```bash
python scripts/hydrate_real_video_corpus.py
cvbench run --benchmark benchmarks/real-video-v2.yaml \
  --system systems/real-video-baseline-local.yaml --output runs/
```

The motion baseline is a transparent lower bound, not a learned detector. It
is expected to expose failures on camera motion, stationary targets,
occlusion, and ambiguous person-versus-vehicle geometry.
