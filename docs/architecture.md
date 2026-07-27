# Architecture

CVBench Version 1 is intentionally one installable package and one runner process.

```text
CLI -> runner -> runtime adapter (local process or Docker)
              -> progressive Unix socket feed
              -> stdout JSONL collector + external timestamps
              -> process/container resource sampler
              -> deterministic matcher -> metrics -> diagnostics
              -> JSON, HTML, and evidence artifacts
```

The boundaries are modules, not services:

- `protocol` validates frame/track/ground-truth data and owns binary framing.
- `scenario` loads immutable frames, annotations, and declared fault actions.
- `runtime` starts a local process or an isolated Docker container.
- `collector` is the only code that timestamps and accepts SUT output.
- `matching` implements deterministic, gated Hungarian assignment.
- `metrics` contains pure scoring logic.
- `resources` samples process trees or Docker stats and optionally NVIDIA metrics.
- `diagnostics` derives structured interpretations from measurements without presenting possible causes as facts.
- `reporting` and `evidence` persist portable artifacts.

Core scoring has no dependency on Docker APIs, Cloudflare, a database, or a UI. Adding a runtime means implementing process startup and resource targeting; adding a report format does not alter matching.

## Clock model

For online replay, the runner schedules each frame against `time.monotonic_ns()` and places that scheduled absolute timestamp in the frame metadata. The collector calls the same external clock immediately after reading each stdout record. Authoritative latency is receive time minus the referenced frame time. Scenario-relative timestamps remain in provenance and shifted ground truth.

Offline debug mode is accepted for format and throughput work, but its report marks latency non-authoritative.
