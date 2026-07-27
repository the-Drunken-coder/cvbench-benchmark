# Unix-domain-socket frame protocol

Version 1 uses a progressive Unix-domain stream socket. It is local, easy to mount into a container, not exposed on a network, and prevents access to future frames. The temporary directory and socket are owner-only (`0700` and `0600`); Docker runs with that owner's numeric UID/GID. The runner sends exactly one message at a time in source order.

Each message is:

```text
4-byte unsigned big-endian JSON length
4-byte unsigned big-endian payload length
UTF-8 JSON metadata
payload bytes
```

Limits are 1 MB of metadata and 100 MB of payload. Frame messages carry JPEG bytes and the canonical metadata fields from `schemas/frame-v1.schema.json`. `native_source_timestamp_ns` is immutable scenario-relative camera truth. `source_timestamp_ns` is the run-scoped causal frame identity that outputs echo; it preserves native timestamp deltas and is never treated as a self-reported processing clock. The trusted runner records the successful complete-frame send boundary separately and uses that delivery timestamp as the online latency origin. Control events are `stream_start`, `stream_end`, `feed_interruption_start`, `feed_interruption_end`, and `benchmark_end`.

The input socket is supplied in `CVBENCH_INPUT_SOCKET`. A SUT connects, then prints `CVBENCH_READY` to stdout. Every subsequent non-empty stdout line must be one complete `cvbench.track/v1` JSON object. Diagnostic text belongs on stderr. Malformed stdout is recorded as a protocol failure rather than silently skipped. The collector reads fixed-size byte chunks and enforces configured per-line, total-byte, total-record, and records-per-second limits before JSON decoding. After flushing its final stdout record, a SUT may half-close its write side of the independent input socket. The runner drains and acknowledges stdout already written before that signal, then closes scoring; a clean process exit does not bypass this drain. Definitive stdout EOF also completes the boundary. If neither completion signal is observed by the bounded scoring-drain deadline, the run fails; stdout arriving after the acknowledged boundary is ignored. Every socket send is bounded by the positive remaining overall run budget, so an exhausted budget fails the run immediately and cleanup proceeds outside the scoring interval.

Track boxes use `[x_min, y_min, x_max, y_max]` in source pixels. `observed` means supported by the current image; prediction and coasting must use `predicted`. The runner never accepts SUT timing as authoritative. Output naming a pending frame remains quarantined until its complete send succeeds; guessed future, unknown, and failed-delivery identities are rejected.
