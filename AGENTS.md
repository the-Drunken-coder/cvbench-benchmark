Simple, elegant solutions are always superior to complex ones.

This repository owns benchmark execution, scoring, reports, the trusted runner,
and the control plane.

- Do not add dataset annotation, reconciliation, review, or release-authoring
  workflows here.
- Consume new datasets only through immutable, SHA-256-pinned release locks
  documented in `datasets/README.md`.
- Treat `scenarios/real-video-v2` as a frozen compatibility fixture. Do not edit
  its truth or promote an experimental `real-video-v3` corpus here.
- Preserve the `public-whole-system-v3.yaml` expanded compute/runtime contract
  unless a separately versioned benchmark migration explicitly replaces it.
- Keep submitted systems isolated from ground truth and future frames.
