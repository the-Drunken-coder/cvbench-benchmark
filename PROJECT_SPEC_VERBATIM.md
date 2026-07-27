# Computer Vision System Benchmarking Platform

## Version 1 Implementation Specification

## 1. Purpose

Build a local-first benchmarking system for evaluating complete computer vision systems that process a camera or video feed and emit standardized tracking output.

The system under test may contain any combination of:

* Neural-network models
* Classical computer vision
* Optical flow
* Kalman filters
* Rule-based logic
* Object association logic
* Persistent track storage
* Multiple cooperating processes
* Backup tracking methods
* Hardware-accelerated components

The benchmark must treat the entire camera-input-to-track-output stack as a black box.

Version 1 is primarily intended for use by AI coding agents iteratively developing and testing computer vision systems.

The benchmark must provide enough feedback to identify real system failures without encouraging the coding agent to make random or unjustified changes.

---

# 2. Version 1 Scope

Version 1 must deliver:

* A local benchmark runner
* A clearly defined system-under-test interface
* Online video playback without future-frame access
* Standardized streaming tracking output
* Automated metric calculation
* Machine-readable diagnostic findings
* Human-readable benchmark reports
* Resource monitoring
* Baseline-versus-candidate comparison
* A modular architecture that can later support:

  * Cloudflare control plane
  * Remote runners
  * Web dashboard
  * Additional input protocols
  * Additional tracking formats
  * Hardware-in-the-loop testing
  * Synthetic scenario generation

Version 1 does not need to include:

* Cloudflare deployment
* Hosted benchmark infrastructure
* User accounts
* Public leaderboards
* Distributed job scheduling
* GPU cloud provisioning
* Multi-user permissions
* Billing
* A production web interface

A minimal local report viewer is optional. Static HTML report generation is acceptable.

---

# 3. Primary Benchmark Goals

Version 1 must measure three major categories:

1. Tracking accuracy
2. System robustness
3. Latency

Resource usage must also be collected because efficiency is a stated design goal, even though it is not one of the three primary scoring categories.

The benchmark must not reduce all results to one opaque score.

Each category must be reported separately.

---

# 4. Definition of the System Under Test

The system under test, abbreviated SUT, is the complete application being evaluated.

The SUT receives a video stream or frame stream and emits track updates while the input is being delivered.

The benchmark must not require the SUT to expose internal model details.

The SUT may be:

* A Docker or OCI container
* A local executable
* A command-line process
* A multi-process application launched by a wrapper command

Version 1 should prioritize Docker or OCI container execution because it provides:

* Reproducible dependencies
* Process isolation
* Easier resource accounting
* Easier network disabling
* Consistent filesystem mounting

A local-process adapter may also be implemented for development convenience.

---

# 5. Critical Real-Time Constraint

The benchmark must evaluate the SUT as an online system.

The SUT must not receive direct filesystem access to the complete source video during scored online runs.

The benchmark runner must deliver input progressively so the SUT cannot inspect future frames.

The benchmark must support at least two modes:

## 5.1 Online Replay Mode

Input is delivered according to the source timestamps or a defined playback schedule.

This mode is used to measure:

* Detection reaction speed
* Acquisition latency
* End-to-end latency
* Tracking continuity
* Reacquisition behavior
* Real-time stability
* Queue buildup
* Deadline misses

## 5.2 Offline Debug Mode

The complete file may be available to the SUT.

This mode is only for:

* Development
* Debugging
* Output format validation
* Maximum-throughput testing

Offline results must never be presented as online latency results.

---

# 6. Version 1 Input Interface

Version 1 must support one canonical input interface.

Recommended initial interface:

* A local HTTP, WebSocket, or Unix-domain-socket frame stream
* Frames transmitted as encoded JPEG or raw RGB
* Each frame accompanied by authoritative source timestamps and metadata

An encoded RTSP or RTP stream may be added later, but should not be required for the first implementation unless the selected SUT already requires it.

The initial input protocol must provide:

```json
{
  "sequence_id": "sequence_001",
  "frame_index": 152,
  "source_timestamp_ns": 5066666667,
  "width": 1920,
  "height": 1080,
  "pixel_format": "rgb24",
  "payload_encoding": "jpeg"
}
```

The actual image bytes may be transmitted:

* In the same binary message
* As multipart content
* Through shared memory with a metadata channel

The implementation should select one approach and document it clearly.

The benchmark must preserve:

* Frame order
* Source timestamps
* Frame dimensions
* Frame identity
* Stream start and end events

The benchmark must be able to intentionally produce:

* Dropped frames
* Delayed frames
* Duplicate frames
* Black frames
* Feed interruptions

These fault injections may be implemented after the basic streaming path works, but the architecture must allow them.

---

# 7. Version 1 Output Interface

The SUT must emit track events during execution.

JSON Lines is the canonical Version 1 format.

Each output record must contain:

```json
{
  "schema_version": "cvbench.track/v1",
  "event": "track_update",
  "sequence_id": "sequence_001",
  "source_timestamp_ns": 5066666667,
  "track_id": "track_17",
  "state": "confirmed",
  "support": "observed",
  "class_id": "person",
  "confidence": 0.94,
  "geometry": {
    "type": "bbox_xyxy",
    "space": "source_pixels",
    "value": [403.2, 188.1, 527.6, 309.4]
  }
}
```

Supported event types in Version 1:

* `track_started`
* `track_update`
* `track_ended`
* `system_status`
* `system_error`

Required track states:

* `tentative`
* `confirmed`
* `coasting`
* `reacquired`
* `lost`

Required support values:

* `observed`
* `predicted`

The distinction between `observed` and `predicted` is mandatory.

A system must not report a predicted track as a current visual observation.

The collector must add an external receive timestamp:

```json
{
  "collector_received_timestamp_ns": 5098421290,
  "system_record": {
    "event": "track_update",
    "source_timestamp_ns": 5066666667
  }
}
```

Externally measured latency must use the collector timestamp rather than a timestamp self-reported by the SUT.

---

# 8. Track Geometry

Version 1 must support axis-aligned bounding boxes.

Canonical format:

```text
[x_min, y_min, x_max, y_max]
```

Coordinates must use source-image pixels.

Requirements:

* Floating-point coordinates are allowed.
* Coordinates may not be NaN or infinite.
* Coordinates must be ordered correctly.
* Boxes outside the frame must be either clipped or rejected according to benchmark configuration.
* The output schema must reject malformed geometry.

Future versions may support:

* Segmentation masks
* Keypoints
* Rotated boxes
* World coordinates
* Bearing and range
* Multi-camera fusion

These should not complicate Version 1.

---

# 9. Ground Truth Format

Ground truth must represent more than object boxes.

Each annotated object must include:

```json
{
  "target_id": "gt_12",
  "sequence_id": "sequence_001",
  "source_timestamp_ns": 5066666667,
  "on_screen": true,
  "eligible_for_detection": true,
  "visibility_fraction": 0.83,
  "occlusion": "partial",
  "class_id": "person",
  "bbox_xyxy": [402.0, 187.0, 528.0, 310.0]
}
```

Required ground-truth concepts:

* Persistent physical target identity
* Whether the target is on screen
* Whether the target is eligible for detection
* Visibility state
* Occlusion state
* Bounding box
* Target class
* Entry event
* Exit event
* Vision-loss intervals
* Reappearance events

An object must not automatically count as detectable from the first frame containing any visible pixels.

The benchmark must support an explicit `eligible_for_detection` flag so acquisition timing begins at a fair point.

---

# 10. Tracking Accuracy Metrics

Version 1 must calculate at least the following.

## 10.1 Acquisition Rate

Percentage of eligible targets that become correctly tracked.

Report:

* Total eligible targets
* Acquired targets
* Never-acquired targets
* Acquisition rate

## 10.2 Acquisition Latency

For each target:

```text
acquisition latency =
time of first correct confirmed track
-
time target became eligible
```

Report:

* Median
* p90
* p95
* p99
* Maximum
* Percentage acquired within configurable deadlines

Default deadlines:

* 100 ms
* 250 ms
* 500 ms
* 1000 ms

## 10.3 Visible Observation Coverage

Measure the percentage of eligible visible target time during which the SUT emits a correct observed update.

Predicted or coasting updates must not count as observed coverage.

Report:

* Overall observed coverage
* Coverage per target
* Coverage by scenario
* Coverage by visibility level

## 10.4 Track Continuity

Measure the percentage of eligible target time during which either:

* A correct observed track exists
* A correct predicted or coasting track exists

Observed coverage and continuity must be reported separately.

## 10.5 Visible Dropouts

A visible dropout occurs when:

* The target remains on screen
* The target remains eligible
* No correct observed output is produced for longer than the configured tolerance

Report:

* Dropouts per target-minute
* Median dropout duration
* p95 dropout duration
* Longest dropout
* Number of dropouts exceeding:

  * 100 ms
  * 250 ms
  * 500 ms
  * 1000 ms

## 10.6 Localization Accuracy

Report:

* Intersection over Union
* Center-position error in pixels
* Normalized center-position error
* Width and height error
* Accuracy by visibility
* Accuracy by target size

## 10.7 Identity Integrity

Report:

* ID switches
* Track fragmentation
* Duplicate tracks
* Track merges
* Track splits
* ID switches per target-minute or target-hour

The matching implementation must be deterministic and documented.

## 10.8 Multi-Target Performance

Report metrics as target count increases.

At minimum, benchmark results must be grouped by:

* 1 target
* 2 targets
* 4 targets
* 8 or more targets when available

---

# 11. False Detection Metrics

Version 1 must distinguish one-frame false detections from persistent false tracks.

Report:

* False detections per camera-minute
* False track births per camera-hour
* False track duration
* False track-seconds per camera-hour
* Longest-lived false track
* Duplicate tracks per real target
* High-confidence false detections
* False detections by scenario type

Long empty or distractor-only sequences must be included.

A tracker must not receive an artificially good score by keeping every target alive indefinitely.

---

# 12. Robustness Metrics

Version 1 must evaluate at least the following robustness behaviors.

## 12.1 Occlusion Survival

For controlled vision-loss intervals, report:

* Whether the track remains active
* Whether the same track ID is preserved
* Position error during the gap
* Whether confidence decreases
* Reacquisition success
* Reacquisition latency
* Wrong-target association after the gap

Test gap durations should include:

* 100 ms
* 250 ms
* 500 ms
* 1000 ms
* 2000 ms

Longer gaps may be added later.

## 12.2 Reacquisition

A correct reacquisition requires:

* The target becomes visible or eligible again
* The SUT produces an observed track
* The track is associated with the correct physical target
* The association occurs within the configured deadline

Report:

* Same-ID reacquisition rate
* Correct-target reacquisition rate
* Reacquisition latency
* Reacquisition by gap duration
* Reacquisition after full occlusion
* Reacquisition after feed interruption
* Reacquisition after visible detector dropout

## 12.3 Feed Faults

The architecture must support testing:

* Frame drops
* Duplicate frames
* Delayed frames
* Black frames
* Temporary feed interruption

Version 1 should implement at least:

* Frame-drop test
* Short blackout test
* Feed interruption test

## 12.4 Long-Running Stability

A long-duration benchmark must test:

* Memory growth
* Latency drift
* Track-ID exhaustion
* State contamination
* Gradual false-positive accumulation
* Recovery after interruption

The initial long-duration test may use concatenated shorter sequences.

---

# 13. Latency Metrics

All authoritative latency measurements must be made externally.

Primary end-to-end latency:

```text
collector receive timestamp
-
source frame timestamp
```

Report:

* First tentative detection latency
* First confirmed acquisition latency
* Per-update latency
* Median latency
* p90 latency
* p95 latency
* p99 latency
* Maximum latency
* Deadline-miss rate
* Latency over time
* Latency by target count
* Latency under fault injection

The runner should also measure:

* Process startup time
* Time to first output
* Input queue depth when available
* Dropped input frames
* Output update rate

The SUT may optionally report internal timings, but these must be labeled as self-reported and non-authoritative.

---

# 14. Resource Monitoring

Version 1 must measure the complete SUT process group or container.

Collect:

* CPU utilization
* CPU time
* Average RAM
* Peak RAM
* GPU utilization when available
* Peak VRAM when available
* Disk read and write
* Network activity
* Process count
* Thread count where practical
* Runtime duration

Reports should include:

* Resource usage over time
* Resource usage by scenario
* Resource usage at different target counts
* Resource usage during overload
* Memory growth during long runs

The resource-monitoring interface must be modular because support will vary by platform.

Required initial platform:

* Linux with Docker or compatible OCI runtime

Other operating systems may be supported later.

---

# 15. Diagnostic Feedback Requirements

The benchmark must not return only scores.

Each significant failure should produce a structured finding.

Required finding structure:

```json
{
  "finding_id": "VIS-DROPOUT-001",
  "category": "tracking_continuity",
  "severity": "high",
  "confidence": "high",
  "status": "confirmed",
  "observation": {
    "visible_target": true,
    "dropout_duration_ms": 621,
    "track_state": "coasting",
    "latency_p99_ms": 714,
    "cpu_utilization": 0.99
  },
  "interpretation": {
    "statement": "The visible tracking dropout coincided with processing backlog.",
    "evidence": [
      "Target remained eligible and visible.",
      "Input-to-output latency increased before the dropout.",
      "CPU utilization reached the configured limit."
    ]
  },
  "possible_causes": [
    "Frame processing cannot sustain input rate.",
    "Input buffering allows stale frames to accumulate."
  ],
  "recommended_test": "cpu-and-input-rate-sweep"
}
```

Findings must separate:

* Observation
* Interpretation
* Possible cause
* Recommended test

The benchmark must not present a speculative cause as a confirmed fact.

---

# 16. Failure Evidence Packets

For every major failure in local mode, the benchmark should generate an evidence packet containing:

* Relevant video segment
* Ground-truth overlay
* SUT-output overlay
* Track-state timeline
* Input timestamps
* Output timestamps
* Latency timeline
* CPU timeline
* Memory timeline
* Matching decisions
* Failure reason
* Reproduction command

Evidence packets may be stored as:

```text
runs/<run_id>/failures/<finding_id>/
```

Suggested contents:

```text
input_clip.mp4
ground_truth.jsonl
system_output.jsonl
overlay.mp4
timeline.json
resources.csv
finding.json
README.md
```

An overlay video is highly desirable but may be deferred until after metric correctness is established.

---

# 17. Baseline Comparison

Every run should optionally identify a baseline system revision.

The report must show:

* Improvements
* Regressions
* Unchanged metrics
* Inconclusive changes

Example:

```json
{
  "metric": "visible_observation_coverage",
  "baseline": 0.973,
  "candidate": 0.991,
  "delta": 0.018,
  "direction": "improvement"
}
```

Regression analysis must include:

* Accuracy
* False detections
* Identity integrity
* Reacquisition
* Latency
* Resource usage

The benchmark should not call a small change meaningful unless enough test evidence exists.

Statistical confidence intervals are desirable, but Version 1 may begin with sample counts and clearly labeled low-confidence results.

---

# 18. Scenario Pack Requirements

Version 1 should ship with a small public scenario pack.

Required scenario families:

## 18.1 Acquisition

* Target enters the frame
* Target begins small
* Target begins partially occluded
* Multiple targets enter close together

## 18.2 Visible Retention

* Target remains clearly visible
* Target crosses complex background
* Target changes scale
* Target approaches frame edge
* Camera motion occurs

## 18.3 Occlusion and Reacquisition

* Partial occlusion
* Full occlusion
* Short blackout
* Target continues predictable motion
* Target changes direction while hidden

## 18.4 Multi-Target Identity

* Targets cross
* Similar-looking targets overlap
* Targets separate after overlap
* One target exits during overlap

## 18.5 False Detection

* Empty scene
* Moving shadows
* Reflections
* Moving vegetation
* Compression artifacts
* Non-target moving objects

## 18.6 Resource Stress

* Increasing target count
* Increasing frame rate
* Increasing resolution when supported
* Artificial CPU restriction
* Long-duration sequence

The initial scenarios may use recorded video, synthetic video, or both.

---

# 19. Test Configuration

A benchmark configuration should be declarative.

Example:

```yaml
schema_version: cvbench.benchmark/v1
id: persistent-target-tracking
version: 0.1.0

input:
  mode: online_replay
  protocol: frame_socket_v1
  playback_rate: 1.0

output:
  protocol: jsonl
  schema: cvbench.track/v1

thresholds:
  confirmed_track_min_duration_ms: 250
  visible_dropout_tolerance_ms: 100
  max_match_center_error_px: 50
  minimum_match_iou: 0.3

scenarios:
  - acquisition
  - visible_retention
  - occlusion
  - multi_target
  - false_detection
  - resource_stress

resources:
  cpu_limit: 4
  memory_limit_mb: 8192
  network_access: false

reporting:
  generate_json: true
  generate_html: true
  generate_failure_packets: true
```

No important metric threshold should be hard-coded inside route handlers or runner logic.

---

# 20. Local Runner Workflow

Expected command:

```bash
cvbench run \
  --benchmark benchmarks/persistent-target-tracking.yaml \
  --system systems/example-system.yaml \
  --output runs/
```

Expected execution flow:

```text
1. Validate benchmark configuration
2. Validate system configuration
3. Start resource monitor
4. Start system under test
5. Wait for readiness signal
6. Begin feed delivery
7. Collect live outputs
8. Record external timestamps
9. End feed
10. Allow defined shutdown grace period
11. Stop system
12. Calculate matches and metrics
13. Generate findings
14. Compare against baseline if provided
15. Generate JSON and HTML reports
16. Generate evidence packets
```

The runner must handle:

* SUT crashes
* SUT hangs
* Invalid output
* Output flooding
* Missing readiness
* Missing shutdown
* Timeouts
* Resource-limit violations

---

# 21. System Definition File

Example:

```yaml
schema_version: cvbench.system/v1
id: example-tracker
revision: local-dev-17

runtime:
  type: docker
  image: example-tracker@sha256:REQUIRED_DIGEST
  command:
    - /app/run
    - --input
    - unix:///run/cvbench/input.sock
    - --output
    - /output/tracks.jsonl

readiness:
  type: stdout_pattern
  pattern: CVBENCH_READY
  timeout_seconds: 30

shutdown:
  grace_period_seconds: 10

resources:
  cpu_limit: 4
  memory_limit_mb: 8192
  network_access: false
```

Mutable image tags such as `latest` must not be accepted for recorded benchmark results unless the resolved immutable image digest is stored in the report.

---

# 22. Architecture Requirements

The project must use modular boundaries.

Recommended package structure:

```text
cvbench/
├── apps/
│   ├── cli/
│   ├── local-runner/
│   └── report-viewer/
│
├── packages/
│   ├── core/
│   ├── protocol/
│   ├── scenario-engine/
│   ├── feed-emulator/
│   ├── output-collector/
│   ├── matcher/
│   ├── metrics/
│   ├── diagnostics/
│   ├── resource-monitor/
│   ├── reporting/
│   └── runtime-docker/
│
├── schemas/
├── benchmarks/
├── examples/
└── tests/
```

Core domain logic must not depend directly on:

* Docker APIs
* Cloudflare APIs
* Filesystem implementation details
* A specific database
* A specific reporting UI

Use interfaces for:

* Runtime execution
* Media delivery
* Output collection
* Resource collection
* Artifact storage
* Metric calculation
* Report generation

---

# 23. Design Principles

The coding agent must follow these rules:

1. Build a benchmark for complete systems, not only neural models.
2. Preserve online behavior and prevent future-frame access in scored runs.
3. Externally timestamp outputs.
4. Separate observed tracking from predicted tracking.
5. Keep metric calculations deterministic.
6. Keep scoring logic independent from execution logic.
7. Keep diagnostic inference separate from raw observations.
8. Avoid one opaque overall score.
9. Prefer declarative benchmark configuration.
10. Keep interfaces extensible without prematurely building every possible feature.
11. Do not create a microservice architecture for Version 1.
12. Do not add Cloudflare-specific code to the core packages.
13. Do not build a large web dashboard before the runner and metrics are correct.
14. Do not optimize visual polish before validating metric correctness.
15. Do not silently tolerate invalid tracking output.
16. Do not trust SUT-reported latency or resource usage.
17. Store complete run provenance.
18. Make every failure reproducible where possible.

---

# 24. Testing Requirements for the Benchmark Itself

The benchmark implementation must have automated tests.

Required tests:

* Output schema validation
* Timestamp handling
* Bounding-box validation
* Matching correctness
* Acquisition-latency calculation
* Visible-dropout calculation
* Reacquisition calculation
* ID-switch calculation
* False-track duration calculation
* Resource-data parsing
* Baseline comparison
* SUT crash handling
* Timeout handling
* Malformed output handling

Metric tests must use small hand-calculated fixtures.

Example:

```text
Target eligible at 1.000 s
Correct confirmed track at 1.250 s
Expected acquisition latency: 250 ms
```

The test must assert exactly 250 ms.

Golden tests should verify complete report output for a small known scenario.

---

# 25. Deliverables

Version 1 is complete when it includes:

* Working local CLI
* Docker-based SUT runner
* Online progressive frame delivery
* JSONL output collector
* External timestamp collection
* Ground-truth schema
* Track matching
* Acquisition metrics
* Visible coverage metrics
* Dropout metrics
* False-detection metrics
* Identity metrics
* Reacquisition metrics
* Latency metrics
* CPU and memory monitoring
* Structured diagnostic findings
* JSON report
* Human-readable HTML or Markdown report
* Baseline comparison
* At least one complete example system
* At least one intentionally broken example system
* At least one public benchmark scenario pack
* Automated metric tests
* Clear developer documentation

---

# 26. Explicit Non-Goals for Version 1

The coding agent must not spend Version 1 effort on:

* Public hosting
* Cloudflare Workers
* User authentication
* Payments
* Public leaderboards
* Social features
* Large-scale distributed execution
* Kubernetes
* Full production web UI
* Training models
* Automatically changing the SUT code
* Supporting every annotation format
* Supporting every video protocol
* Supporting every operating system
* Perfect anti-cheating protection
* A universal overall score

---

# 27. Unresolved Decisions

The following decisions remain open and must not be invented silently.

## 27.1 Canonical Input Transport

Select one:

* Unix-domain-socket frame stream
* WebSocket frame stream
* Local HTTP multipart stream
* RTSP stream
* Shared-memory frame ring

Recommended Version 1 default:

* Unix-domain socket for local Docker execution
* JPEG or raw-frame payloads
* Explicit timestamp metadata

Reason:

* Easier to implement and control than RTSP
* Supports progressive delivery
* Avoids network exposure
* Keeps future-frame access blocked

## 27.2 Annotation Source

Determine whether the first scenario pack will use:

* Manually annotated real videos
* Existing public tracking datasets
* Synthetic generated videos
* A combination

Recommended:

* Begin with a small public tracking dataset or hand-annotated clips
* Add synthetic controlled occlusion tests later

## 27.3 Target Classes

Determine whether Version 1 is:

* Class-agnostic tracking
* Person tracking
* Vehicle tracking
* Mixed-object tracking

Recommended architecture:

* Support class IDs in the schema
* Allow class-agnostic matching through configuration
* Do not hard-code one object class

## 27.4 Reentry Identity Policy

When a target leaves the frame and later returns, determine whether:

* It must keep the same identity
* It may receive a new identity
* Both modes are benchmarked separately

Recommended:

* Treat same-scene short reentry as a configurable test
* Do not make it part of the default tracking score until explicitly defined

## 27.5 Matching Rules

Define:

* Minimum IoU
* Maximum center distance
* Class compatibility rules
* Matching algorithm
* Handling of predicted tracks during occlusion
* Tie-breaking behavior

Recommended:

* Hungarian assignment
* Configurable IoU and center-distance gating
* Deterministic tie-breaking
* Separate observed and predicted matching results

## 27.6 Readiness Interface

Determine how the SUT reports it is ready:

* Standard output line
* File creation
* Health endpoint
* Socket connection

Recommended Version 1 default:

```text
CVBENCH_READY
```

on standard output.

## 27.7 GPU Monitoring

Determine required GPU vendors.

Recommended Version 1:

* Implement an interface
* Support NVIDIA through `nvidia-smi` when available
* Continue without GPU metrics when unavailable
* Clearly mark missing metrics

---

# 28. Assumptions Being Challenged

## Assumption: A final output file is sufficient

Rejected.

A final file alone cannot prove online behavior or reaction speed. Live output capture is required.

## Assumption: Tracking continuity and visual observation are the same

Rejected.

A system may successfully predict through a detector dropout. Both observed coverage and total continuity must be reported.

## Assumption: Keeping tracks alive longer is always better

Rejected.

Long persistence may create ghost tracks. Survival and timely termination must both be scored.

## Assumption: One tracking metric can represent system quality

Rejected.

Acquisition, localization, identity, continuity, false tracks, reacquisition, latency, and resources must remain visible.

## Assumption: The system itself can report accurate latency

Rejected.

Latency must be externally measured.

## Assumption: Model internals should determine the benchmark interface

Rejected.

The benchmark must operate against the complete system boundary.

## Assumption: Maximum FPS represents real-time quality

Rejected.

A system may have good average throughput while accumulating latency or dropping observations. Queueing and deadline behavior must be measured.

## Assumption: More architecture is automatically more future-proof

Rejected.

Version 1 should use clean interfaces and modular packages, but avoid unnecessary services, distributed systems, and abstraction layers without a concrete second implementation.

---

# 29. Recommended Implementation Order

## Phase 1: Protocol and deterministic metrics

Build:

* Schemas
* Ground-truth format
* Output validator
* Matching engine
* Hand-calculated metric tests

## Phase 2: Local online runner

Build:

* Docker execution
* Progressive input delivery
* Live output collection
* External timestamping
* Crash and timeout handling

## Phase 3: Core reports

Build:

* Acquisition
* Visible coverage
* Dropouts
* False detections
* Localization
* Identity
* Latency

## Phase 4: Robustness

Build:

* Occlusion intervals
* Reacquisition
* Frame dropping
* Blackouts
* Feed interruption

## Phase 5: Resources and diagnostics

Build:

* CPU and memory monitoring
* NVIDIA monitoring when available
* Structured findings
* Baseline comparison
* Failure packets

## Phase 6: Refinement

Build:

* Static HTML report
* Overlay rendering
* Additional scenario packs
* Long-duration and overload testing

---

# 30. Acceptance Example

Given a test sequence where:

* Target becomes eligible at 1.000 seconds
* SUT emits a confirmed correct track at 1.220 seconds
* Target remains visible until 10.000 seconds
* SUT has a visible observed-output gap from 4.000 to 4.350 seconds
* SUT maintains a correctly predicted track during the gap
* Target is fully occluded from 6.000 to 7.000 seconds
* SUT preserves the track ID
* SUT emits a correct observed update at 7.180 seconds
* One false track exists for 2.0 seconds

The benchmark must report:

```text
Acquisition latency:             220 ms
Acquired within 250 ms:          yes
Visible observed dropout:        350 ms
Observed coverage:               reduced by visible gap
Track continuity during gap:     maintained
Occlusion survival:              pass
Same-ID reacquisition:           pass
Reacquisition latency:           180 ms
False track duration:            2.0 seconds
```

The diagnostic report must not misclassify the visible observed-output gap as complete tracking loss because the predicted track remained valid.

---

# 31. Working Project Name

Temporary name:

**CVBench System Runner**

The name should remain easy to replace. Do not embed it unnecessarily into protocol field names, database structures, or package boundaries.
