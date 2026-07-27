# Timing and compute contract

`cvbench.timing-compute/v1` keeps camera truth, delivery pace, and system work as separate clocks. A system may need more time, but slower delivery or completion never changes the native source FPS or hides compute cost.

## Clock model

Every frame has one immutable native timestamp from the scenario. The runner creates a run-scoped source epoch and adds the native relative timestamp without scaling it. Frame metadata also carries `native_source_timestamp_ns`, and each `stream_start` states native frame count, duration, and nominal FPS.

The delivery clock is independent:

```text
scheduled delivery = delivery epoch + native relative timestamp / replay rate
source timestamp    = source epoch   + native relative timestamp
```

The completion clock is external `time.monotonic_ns()` observed by the runner and output collector. This provides:

- startup duration;
- stream-delivery duration;
- bounded post-stream drain duration;
- total wall and completion duration;
- runner teardown duration, reported separately and excluded from scoring completion/drain;
- real-time factor = completion seconds / native source seconds;
- non-scoring native-source offset and scored processing latency after successful frame delivery.

Frames stay ordered and progressive. The SUT receives no future frame, scenario annotation, scoring ROI, or ground-truth hint. Its socket remains open across frames so temporal state and multiple cooperating processes are allowed.

## Replay profiles

Benchmark YAML selects `input.replay_profile`, not an arbitrary value:

| Profile | Exact rate | Leaderboard use |
| --- | ---: | --- |
| `quarter-speed` | 0.25 | Separate quarter-speed class |
| `half-speed` | 0.5 | Separate half-speed class |
| `native` | 1.0 | Standard native real-time capability |
| `accelerated-test-20x` | 20.0 | Test-only, ineligible |
| `accelerated-test-100x` | 100.0 | Test-only, ineligible |

For configuration compatibility, `input.playback_rate` is still accepted only when it exactly matches this versioned allowlist. When both fields appear they must agree. The selected profile, exact rate, timing contract, and delivery policy are part of the comparison fingerprint and provenance.

Public `/api/v1` submissions remain compatible and fixed to `native` at 1.0x. The request does not accept a benchmark, replay, environment, or shell override. A slower public category would require a new fixed suite/class assignment; it cannot enter the native class through a request parameter.

## Backpressure and causal output

`cvbench.delivery-lossless/v1` uses an independent source schedule and ordered socket delivery:

- a slow reader never moves source timestamps or changes native FPS;
- ordinary pacing never intentionally drops a frame;
- declared fault-injection drops remain explicit;
- sender calls over 5 ms are transport-pressure samples;
- delivery backlog is measured from the independent scheduled time;
- a frame misses its delivery deadline when completion is later than one scheduled median frame interval, with a 5 ms minimum tolerance;
- portable Unix sockets do not expose a reliable queue-depth value, so queue depth is reported unavailable rather than invented.

Per-frame delivery records retain native timestamp, scheduled offset, successful scoring-delivery offset, sender-call duration, backlog, delivery status, drop reason, and deadline status. `processing_latency_ms` starts at the trusted complete-frame delivery boundary. `native_source_offset_ms` separately shows how far collector receipt is from the immutable native timeline; intentional half/quarter-speed pacing therefore remains visible but cannot trigger SUT latency deadlines.
The aggregate reports both configured replay rate and measured effective replay rate (native source seconds delivered per wall second), plus delivered frames per wall second.

Output is causal only when its `(sequence_id, source_timestamp_ns)` names a frame whose complete socket send succeeded. An immediate output racing an in-flight send is held pending until send success; transport failure rejects it. Guessed future, unknown, failed-delivery, or rewritten timestamps are malformed output. The benchmark-end boundary is recorded only after the potentially blocking marker send succeeds. Exact outputs may arrive after that boundary during the bounded drain window and remain scored with their external latency. The hard overall run deadline, `max_drain_seconds`, record count, line bytes, total stdout bytes, and output-rate limits define scoring; terminate/kill and Docker cleanup time is reported separately as teardown.

## Container compute accounting

Leaderboard resource evidence is read by the trusted host runner directly from a unique runner-owned parent cgroup-v2 scope, never from SUT self-report and never by executing a command inside the submitted image. This works for distroless and `scratch` images. Docker automatic removal is disabled: the retained container metadata supports isolation verification, while an empty runner-owned child cgroup keeps the parent scope addressable without adding a process or resource cost. The parent therefore retains cumulative CPU, memory-peak, and I/O counters even if an immediately exiting container's child cgroup disappears. During bounded drain, a system may half-close its output side of the input socket after its last flushed stdout record while continuing to read until the runner releases EOF. The half-close requests an ordered collector boundary: the runner drains and acknowledges stdout already written before closing scoring. A clean process exit still waits for that acknowledgement or definitive stdout EOF; if neither arrives by the hard drain limit, the run fails. Stdout observed after the acknowledged boundary cannot become scoreable. The runner then stops its sampler, captures a new cumulative parent-scope sample, releases input EOF, and performs teardown. Only after that boundary does deterministic cleanup remove the retained container, empty retention child, and parent scope. A missing final read or failed cleanup makes the run failed and ineligible; a recent ordinary sample is never relabeled as final. Before every frame, duplicate, interruption, stream-control, and benchmark-end send, the runner sets the socket timeout to the positive remaining overall run budget; an exhausted budget fails immediately. Evidence includes:

- wall, startup, delivery, completion, and drain duration;
- cgroup CPU time and CPU-seconds per native source-second;
- average and peak CPU;
- average and peak RAM;
- disk read/write bytes;
- process count, so child work stays charged;
- output records per native source-second;
- per-output processing latency plus delivery backlog/deadline misses.

Local process-tree measurements remain useful for development but are labeled best-effort and are not leaderboard-authoritative. GPU/VRAM values are omitted unless the runner genuinely assigns and isolates a GPU; host-wide `nvidia-smi` snapshots are not treated as a submitted system's cost.

Missing CPU time, CPU-seconds/source-second, real-time factor, average/peak CPU, peak RAM, disk I/O, external-cgroup availability, or the final cumulative sample makes a result leaderboard-ineligible. Sleeping and hidden child processes therefore cannot turn missing accounting into a cheaper eligible result.

Sleeping can reduce CPU-seconds, but it increases real-time factor. Background children stay in the container cgroup. Neither tactic improves every raw efficiency axis.

## Leaderboard semantics

`cvbench.pareto/v1` has no composite score. Accuracy metrics remain unchanged. Results expose raw accuracy and efficiency axes plus a class:

```text
<replay profile>/<CPU tier>/<completion tier>
```

CPU tiers use CPU-seconds per native source-second: `cpu-1` (≤1), `cpu-2` (≤2), `cpu-4` (≤4), and `cpu-over-4`. Completion tiers use real-time factor: `realtime` (≤1.05), `completion-2x` (≤2.05), `completion-4x` (≤4.05), and `completion-over-4x`.

The comparison fingerprint binds the scenario corpus, timing/delivery policy, replay profile/rate, CPU and RAM envelope, run/drain/output budgets, and accounting availability. Only two eligible results with identical non-null fingerprints and identical non-null class IDs are equal-category comparisons. Within a class, one result Pareto-dominates another only when it is no worse on every declared raw axis and strictly better on at least one. Higher accuracy bought with more compute or slower completion therefore remains visible and cannot silently win an equal-budget category.

The fixed public Version 3 Docker envelope is 4 CPUs, 8192 MiB total memory with swap disabled, a 512-process ceiling, no network, one owner-only socket mount, an unprivileged UID/GID, and an immutable image identity. The hard run deadline is 240 seconds; native source time, latency, backlog, and completion efficiency remain measured rather than normalized away.

## Evidence

The Linux Docker gate runs the same scored acquisition workload as:

- the fast OpenCV baseline;
- a 150 ms/frame CPU-bound system;
- a 150 ms/frame idle system;
- the fast system with a CPU-bound child process;
- a system that performs a delayed CPU and synchronous disk-write burst after `benchmark_end`.

`scripts/assert_pacing_evidence.py` requires identical accuracy and native source duration, verifies cgroup authority, proves the slow systems' real-time-factor cost, proves sleeping trades CPU for completion time, proves child CPU/process use remains charged, checks that the newly captured final sample includes the post-stream CPU and disk-write burst, and verifies authoritative accounting for an immediate clean exit. `scripts/run_immediate_exit_e2e.py` deliberately delays the collector's final record through the half-close boundary and asserts scored output plus container/cgroup cleanup. CI publishes the compact `cvbench.timing-compute-evidence/v1` artifact.

Public-safe Docker report artifacts are not mislabeled core reports. Sanitization changes their identity to `cvbench.report-redacted/v1`, records `cvbench.report/v1` as the source contract, and replaces restricted audit and diagnostic sections with versioned redaction markers. Both the sanitizer and upload verifier validate `schemas/report-redacted-v1.schema.json`; an artifact that claims the core report version after redaction is rejected.
