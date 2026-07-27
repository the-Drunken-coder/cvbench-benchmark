# Frozen real-video-v2 compatibility fixture

`real-video-v2` is the historical MEVA-derived runtime dataset used by the
Version 2 real-video benchmark and the current public Version 3 execution
contract. Its three 150-frame, native-30-FPS scenarios remain byte-for-byte
available so existing results and trusted-runner behavior stay reproducible.

This repository does not own the annotation workflow. The former preparation,
normalization, and review scripts moved to the dataset repository boundary.
No label changes should be made here.

The fixture is public evaluation data derived from MEVA material distributed
under CC BY 4.0. It is not model training data supplied by CVBench. Ground truth
is loaded only by the scorer and is never sent to submitted systems.

The compatibility lock is
[`datasets/locks/real-video-v2.compatibility.json`](../datasets/locks/real-video-v2.compatibility.json).
It pins:

- the inline archive manifest;
- the exact 450-frame hash manifest;
- the corpus fingerprint;
- the only permitted runtime install path, `data/real-video-v2`.

Hydrate the frozen fixture with:

```bash
python scripts/hydrate_real_video_corpus.py
```

The hydrator validates the compatibility lock, every frame archive, every frame
hash, and the resulting runtime file manifest. The benchmark manifests continue
to reference the same scenario IDs and ground-truth bytes.

For new or revised datasets, publish a deterministic dataset release and pin it
through the [dataset consumption boundary](../datasets/README.md). Dataset
reconciliation, human review, certification, source acquisition, and release
assembly must not be added to this repository.

`benchmarks/real-video-v2.yaml` remains for historical reproduction.
`benchmarks/public-whole-system-v3.yaml` remains the active expanded compute and
runtime contract; it deliberately continues to use this frozen v2 compatibility
fixture. The rejected experimental reconciled `real-video-v3` truth is not
included.
