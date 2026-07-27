# Learned-model packaging guide

Use this shape for a detector, segmenter, or multimodel tracking pipeline:

```text
your-system/
├── Dockerfile
├── requirements-runtime.txt
├── system.py
├── model.onnx
└── THIRD_PARTY.md
```

`model.onnx` is illustrative. Keep weights outside this repository and copy
them into the final image from your private build context or approved artifact
store.

## Adapter responsibilities

`system.py` must:

1. Load all weights before declaring readiness.
2. Connect to the Unix socket in `CVBENCH_INPUT_SOCKET`.
3. Print exactly `CVBENCH_READY` as one stdout line.
4. Decode each current JPEG without reading future frames.
5. Retain temporal state and assign persistent track IDs.
6. Emit source-pixel `[x_min, y_min, x_max, y_max]` boxes as
   `cvbench.track/v1` JSONL.
7. Mark image-supported output as `observed` and extrapolation as `predicted`.
8. Write human-readable logs only to stderr.

The complete framing and output contract is documented in
[`docs/protocol.md`](../../../docs/protocol.md). The synthetic color tracker is
the shortest working adapter to copy.

## Minimal image shape

```dockerfile
FROM python:3.12-slim
WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt
COPY system.py model.onnx THIRD_PARTY.md ./

RUN groupadd --system cvbench \
    && useradd --system --gid cvbench --home-dir /nonexistent cvbench
USER cvbench
CMD ["python", "/app/system.py"]
```

Build for the public execution platform and verify it locally before
publication:

```bash
docker build --platform linux/amd64 -t your-system:local your-system/
```

The public API accepts only a remotely resolvable immutable reference such as
`ghcr.io/example/your-system@sha256:...`; a local tag or locally calculated
digest is not a submission reference.
