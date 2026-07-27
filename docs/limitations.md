# Version 1 limitations

- Linux Docker is the required runtime for isolated execution. Docker Desktop on macOS cannot pass a host Unix socket through its VM-backed bind mount (`ENOTSUP`); use the local adapter on macOS or a Linux Docker host for scored container runs. Local-process execution also works on Unix for development; Windows named pipes are not implemented.
- GPU metrics use `nvidia-smi` when present. Other GPU vendors are represented by the modular interface but have no collector yet.
- Synthetic data makes correctness and faults reproducible but does not substitute for a domain-specific real-video evaluation pack.
- Evidence MP4 generation depends on the OpenCV build's MP4V writer. All JSON, JSONL, CSV, timelines, matching rationale, and reproduction commands are still generated if that codec is unavailable.
- Comparison confidence is deliberately conservative and sample-count based. Version 1 does not claim statistical significance from a small delta.
- Queue depth is recorded only when exposed by a future transport/runtime adapter. Delivered, intentionally dropped, duplicate, black, interrupted, and delayed counts are externally recorded now.
