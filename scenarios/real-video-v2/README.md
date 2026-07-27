# `real-video-v2` compatibility fixture

These three public MEVA-derived, 30 FPS scenarios are frozen legacy runtime
assets. They remain checked in only to reproduce historical Version 2 and
current public Version 3 runs. They are not a dataset-authoring surface.

New truth is reviewed and released by the separate dataset repository, then
consumed here through a hash-pinned lock. See
[`datasets/README.md`](../../datasets/README.md).

The compatibility lock pins these scenario manifests, ground truth, frame
archives, and hashes. Runtime JPEGs are recreated below ignored
`data/real-video-v2`.

There are no target hints, ignore rows, or scoreable ROIs. See
[`docs/real-video-sources.md`](../../docs/real-video-sources.md) for the frozen
runtime contract.
