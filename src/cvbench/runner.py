from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .audit import build_audit_evidence
from .collector import OutputCollector
from .comparison import compare_reports
from .config import BenchmarkConfig, load_benchmark, load_system
from .diagnostics import generate_findings
from .errors import RuntimeFailure
from .evidence import generate_evidence_packets
from .matching import match_records_by_support
from .metrics import calculate_metrics
from .model import CollectedRecord, RunArtifacts, RuntimeOutcome, Scenario
from .protocol import send_message
from .reporting import write_report_files
from .resources import ResourceMonitor, unavailable_resource_summary
from .runtime import (
    StartedRuntime,
    cleanup_runtime,
    not_started_isolation,
    start_runtime,
    stop_runtime,
    verify_docker_isolation,
)
from .scenario import load_scenario
from .stability import evaluate_long_run_assertions
from .timing import (
    DeliveryRecorder,
    build_leaderboard_semantics,
    build_timing_summary,
    delivery_tolerance_ns,
    native_source_metadata,
)

EVALUATION_ORDER_ALGORITHM = "sha256-sort/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    """Render repository paths without publishing host usernames or worktree roots."""
    parts = path.resolve().parts
    for anchor in ("benchmarks", "systems", "scenarios", "data"):
        if anchor in parts:
            return Path(*parts[parts.index(anchor) :]).as_posix()
    return path.name or "<path>"


def _comparison_fingerprint(
    benchmark: BenchmarkConfig,
    scenarios: list[Scenario],
    system_resources: dict[str, Any] | None = None,
    accounting_availability: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    system_resources = system_resources or {}
    scenario_inputs = []
    for scenario in scenarios:
        manifest = next(path for path in benchmark.scenarios if path.parent == scenario.root)
        frames = sorted(
            scenario.frames,
            key=lambda frame: (frame.frame_index, frame.relative_timestamp_ns, frame.path.name),
        )
        scenario_inputs.append(
            {
                "id": scenario.id,
                "manifest_sha256": _sha256(manifest),
                "ground_truth_sha256": _sha256(
                    scenario.ground_truth_path or scenario.root / "ground_truth.jsonl"
                ),
                "frame_sha256": [
                    {
                        "frame_index": frame.frame_index,
                        "source_timestamp_ns": frame.relative_timestamp_ns,
                        "sha256": _sha256(frame.path),
                    }
                    for frame in frames
                ],
            }
        )
    scenario_inputs.sort(key=lambda item: (item["id"], item["manifest_sha256"], item["ground_truth_sha256"]))
    inputs = {
        "benchmark_id": benchmark.id,
        "benchmark_version": benchmark.version,
        "input_mode": benchmark.input_mode,
        "timing_compute_contract": benchmark.timing_compute_contract,
        "delivery_policy": benchmark.delivery_policy,
        "replay_profile": benchmark.replay_profile,
        "playback_rate": benchmark.playback_rate,
        "resource_envelope": {
            "benchmark": {
                "cpu_limit": benchmark.resources.get("cpu_limit"),
                "memory_limit_mb": benchmark.resources.get("memory_limit_mb"),
                "network_access": benchmark.resources.get("network_access", False),
            },
            "system": {
                "cpu_limit": system_resources.get("cpu_limit"),
                "memory_limit_mb": system_resources.get("memory_limit_mb"),
                "network_access": system_resources.get("network_access", False),
            },
        },
        "run_budgets": {
            "max_run_seconds": benchmark.max_run_seconds,
            "max_drain_seconds": benchmark.max_drain_seconds,
            "max_output_records": benchmark.max_output_records,
            "max_output_line_bytes": benchmark.max_output_line_bytes,
            "max_total_output_bytes": benchmark.max_total_output_bytes,
            "max_output_records_per_second": benchmark.max_output_records_per_second,
        },
        "accounting_availability": accounting_availability,
        "thresholds": asdict(benchmark.thresholds),
        "evaluation_order": {
            "algorithm": EVALUATION_ORDER_ALGORITHM,
            "mode": "configured_seed" if benchmark.evaluation_order_seed is not None else "private_per_run_fallback",
            "seed": benchmark.evaluation_order_seed,
        },
        "scenarios": scenario_inputs,
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), inputs


def _sleep_until(deadline_ns: int, run_deadline: float) -> None:
    while True:
        if time.monotonic() > run_deadline:
            raise TimeoutError("benchmark run deadline expired")
        remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def _send_before_deadline(
    connection: socket.socket,
    metadata: dict[str, Any],
    payload: bytes,
    run_deadline: float,
) -> None:
    remaining = run_deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("benchmark run deadline expired before socket send")
    connection.settimeout(remaining)
    try:
        send_message(connection, metadata, payload)
    except TimeoutError as exc:
        event = metadata.get("event", "message")
        raise TimeoutError(f"benchmark run deadline expired during {event} send") from exc


def _faults_for_frame(scenario: Scenario, frame_index: int) -> list[dict[str, Any]]:
    result = []
    for fault in scenario.faults:
        indices = fault.get("frame_indices", [])
        if frame_index in indices or fault.get("after_frame") == frame_index:
            result.append(fault)
    return result


def _black_jpeg(width: int, height: int) -> bytes:
    ok, encoded = cv2.imencode(".jpg", np.zeros((height, width, 3), dtype=np.uint8))
    if not ok:
        raise RuntimeFailure("could not encode injected black frame")
    return encoded.tobytes()


def _shift_ground_truth(scenario: Scenario, source_base_ns: int) -> list[dict[str, Any]]:
    shifted = []
    for row in scenario.ground_truth:
        record = dict(row)
        record["scenario_source_timestamp_ns"] = row["source_timestamp_ns"]
        record["source_timestamp_ns"] = source_base_ns + row["source_timestamp_ns"]
        record["scenario_family"] = scenario.family
        shifted.append(record)
    return shifted


def _deliver_scenarios(
    connection: socket.socket,
    scenarios: list[Scenario],
    config: BenchmarkConfig,
    run_deadline: float,
    monitor: ResourceMonitor,
    collector: OutputCollector,
    frame_delivery_ns: dict[tuple[str, int], int],
    delivery: DeliveryRecorder,
) -> tuple[
    list[dict[str, Any]],
    dict[str, int],
    dict[str, list[int]],
    dict[tuple[str, int], list[str]],
    DeliveryRecorder,
]:
    shifted_ground_truth: list[dict[str, Any]] = []
    counters = {
        "delivered_frames": 0,
        "dropped_frames": 0,
        "duplicate_frames": 0,
        "black_frames": 0,
        "feed_interruptions": 0,
        "delayed_frames": 0,
    }
    sequence_timestamps: dict[str, list[int]] = {}
    fault_events: dict[tuple[str, int], list[str]] = {}
    for scenario in scenarios:
        if collector.flooded.is_set():
            raise RuntimeFailure(f"output limit exceeded: {collector.limit_reason}")
        source_base_ns = time.monotonic_ns() + 20_000_000
        delivery_base_ns = source_base_ns
        shifted_ground_truth.extend(_shift_ground_truth(scenario, source_base_ns))
        sequence_timestamps[scenario.frames[0].sequence_id] = []
        source_metadata = native_source_metadata([scenario])["sequences"][0]
        _send_before_deadline(
            connection,
            {
                "event": "stream_start",
                "schema_version": "cvbench.frame/v1",
                "sequence_id": scenario.frames[0].sequence_id,
                "timing_compute_contract": config.timing_compute_contract,
                "delivery_policy": config.delivery_policy,
                "replay_profile": config.replay_profile,
                "replay_rate": config.playback_rate,
                "native_source": {
                    "frame_count": source_metadata["frame_count"],
                    "duration_seconds": source_metadata["duration_seconds"],
                    "nominal_fps": source_metadata["nominal_fps"],
                    "timestamp_origin": source_metadata["timestamp_origin"],
                },
            },
            b"",
            run_deadline,
        )
        tolerance_ns = delivery_tolerance_ns(scenario, config.playback_rate)
        for frame in scenario.frames:
            if collector.flooded.is_set():
                raise RuntimeFailure(f"output limit exceeded: {collector.limit_reason}")
            source_timestamp_ns = source_base_ns + frame.relative_timestamp_ns
            scheduled_delivery_ns = delivery_base_ns + int(
                frame.relative_timestamp_ns / config.playback_rate
            )
            sequence_timestamps[frame.sequence_id].append(source_timestamp_ns)
            if config.input_mode == "online_replay":
                _sleep_until(scheduled_delivery_ns, run_deadline)
            if collector.flooded.is_set():
                raise RuntimeFailure(f"output limit exceeded: {collector.limit_reason}")
            fault_actions = _faults_for_frame(scenario, frame.frame_index)
            target_count = sum(
                row["on_screen"] and row["eligible_for_detection"]
                for row in scenario.ground_truth
                if row["source_timestamp_ns"] == frame.relative_timestamp_ns
            )
            monitor.set_context(scenario.family, target_count, bool(fault_actions))
            if fault_actions:
                fault_events[(frame.sequence_id, source_timestamp_ns)] = [
                    str(action.get("type")) for action in fault_actions
                ]
            for fault in fault_actions:
                if fault.get("type") == "feed_interruption":
                    counters["feed_interruptions"] += 1
                    _send_before_deadline(
                        connection,
                        {
                            "event": "feed_interruption_start",
                            "schema_version": "cvbench.frame/v1",
                            "sequence_id": frame.sequence_id,
                            "source_timestamp_ns": source_timestamp_ns,
                        },
                        b"",
                        run_deadline,
                    )
                    interruption_ends_ns = time.monotonic_ns() + int(
                        float(fault.get("duration_ms", 250)) * 1_000_000
                    )
                    _sleep_until(interruption_ends_ns, run_deadline)
                    _send_before_deadline(
                        connection,
                        {
                            "event": "feed_interruption_end",
                            "schema_version": "cvbench.frame/v1",
                            "sequence_id": frame.sequence_id,
                            "source_timestamp_ns": source_timestamp_ns,
                        },
                        b"",
                        run_deadline,
                    )
                elif fault.get("type") == "delay":
                    counters["delayed_frames"] += 1
                    delay_ends_ns = time.monotonic_ns() + int(
                        float(fault.get("duration_ms", 100)) * 1_000_000
                    )
                    _sleep_until(delay_ends_ns, run_deadline)
            if any(fault.get("type") == "frame_drop" for fault in fault_actions):
                counters["dropped_frames"] += 1
                dropped_ns = time.monotonic_ns()
                delivery.record_frame(
                    sequence_id=frame.sequence_id,
                    frame_index=frame.frame_index,
                    native_source_timestamp_ns=frame.relative_timestamp_ns,
                    scheduled_ns=scheduled_delivery_ns,
                    deadline_ns=scheduled_delivery_ns + tolerance_ns,
                    send_started_ns=dropped_ns,
                    send_completed_ns=dropped_ns,
                    delivered=False,
                    drop_reason="fault_injection",
                )
                continue
            payload = frame.path.read_bytes()
            if any(fault.get("type") == "blackout" for fault in fault_actions):
                payload = _black_jpeg(frame.width, frame.height)
                counters["black_frames"] += 1
            metadata = {
                "event": "frame",
                "schema_version": "cvbench.frame/v1",
                "sequence_id": frame.sequence_id,
                "frame_index": frame.frame_index,
                "source_timestamp_ns": source_timestamp_ns,
                "native_source_timestamp_ns": frame.relative_timestamp_ns,
                "width": frame.width,
                "height": frame.height,
                "pixel_format": "rgb24",
                "payload_encoding": "jpeg",
            }
            frame_key = (frame.sequence_id, source_timestamp_ns)
            collector.begin_frame(frame_key, (frame.width, frame.height))
            send_started_ns = time.monotonic_ns()
            delivered = False
            try:
                _send_before_deadline(connection, metadata, payload, run_deadline)
                delivered = True
            finally:
                send_completed_ns = time.monotonic_ns()
                collector.finish_frame(frame_key, delivered=delivered)
                delivery.record_frame(
                    sequence_id=frame.sequence_id,
                    frame_index=frame.frame_index,
                    native_source_timestamp_ns=frame.relative_timestamp_ns,
                    scheduled_ns=scheduled_delivery_ns,
                    deadline_ns=scheduled_delivery_ns + tolerance_ns,
                    send_started_ns=send_started_ns,
                    send_completed_ns=send_completed_ns,
                    delivered=delivered,
                    drop_reason=None if delivered else "transport_failure",
                )
                if delivered:
                    frame_delivery_ns[frame_key] = send_completed_ns
            counters["delivered_frames"] += 1
            if any(fault.get("type") == "duplicate" for fault in fault_actions):
                duplicate = dict(metadata)
                duplicate["duplicate"] = True
                _send_before_deadline(connection, duplicate, payload, run_deadline)
                counters["duplicate_frames"] += 1
        _send_before_deadline(
            connection,
            {
                "event": "stream_end",
                "schema_version": "cvbench.frame/v1",
                "sequence_id": scenario.frames[0].sequence_id,
            },
            b"",
            run_deadline,
        )
    delivery.benchmark_end_send_started_ns = time.monotonic_ns()
    _send_before_deadline(
        connection,
        {"event": "benchmark_end", "schema_version": "cvbench.frame/v1"},
        b"",
        run_deadline,
    )
    delivery.benchmark_end_sent_ns = time.monotonic_ns()
    return shifted_ground_truth, counters, sequence_timestamps, fault_events, delivery


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _restrict_socket_access(socket_dir: Path, socket_path: Path) -> None:
    socket_dir.chmod(0o700)
    socket_path.chmod(0o600)


def _wait_for_readiness(collector: OutputCollector, runtime: StartedRuntime, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if collector.ready.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
            return True
        if collector.flooded.is_set():
            return False
        if runtime.process.poll() is not None:
            return False
    return collector.ready.is_set()


def _finish_scoring(monitor: ResourceMonitor, collector: OutputCollector) -> None:
    collector.close_scoring()
    monitor.finalize_accounting()


def _release_input(connection: socket.socket) -> None:
    with contextlib.suppress(OSError):
        connection.shutdown(socket.SHUT_WR)


def _scoring_complete(connection: socket.socket, collector: OutputCollector) -> bool:
    if collector.stdout_closed.is_set():
        return True
    if collector.output_boundary_drained.is_set():
        return True
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if readable and connection.recv(1, socket.MSG_PEEK) == b"":
            return collector.request_output_boundary()
        return False
    except OSError:
        return collector.request_output_boundary()


def _load_unique_scenarios(
    paths: tuple[Path, ...], run_id: str, evaluation_order_seed: str | int | None = None
) -> list[Scenario]:
    scenarios: list[Scenario] = []
    seen: dict[str, int] = {}
    if evaluation_order_seed is None:
        order_material: dict[str, Any] = {"mode": "private_per_run_fallback", "run_id": run_id}
    else:
        order_material = {
            "mode": "configured_seed",
            "seed": evaluation_order_seed,
            "seed_type": type(evaluation_order_seed).__name__,
        }
    loaded = [(path, load_scenario(path)) for path in paths]

    def order_key(item: tuple[Path, Scenario]) -> tuple[str, str, str]:
        path, scenario = item
        material = {
            "algorithm": EVALUATION_ORDER_ALGORITHM,
            "order_material": order_material,
            "scenario_id": scenario.id,
            "manifest_sha256": _sha256(path),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest(), scenario.id, _sha256(path)

    for _path, scenario in sorted(loaded, key=order_key):
        sequence = scenario.frames[0].sequence_id
        seen[sequence] = seen.get(sequence, 0) + 1
        if seen[sequence] > 1:
            unique_sequence = f"{sequence}__repeat_{seen[sequence]}"
            scenario = Scenario(
                id=f"{scenario.id}__repeat_{seen[sequence]}",
                family=scenario.family,
                root=scenario.root,
                frames=[replace(frame, sequence_id=unique_sequence) for frame in scenario.frames],
                ground_truth=[{**row, "sequence_id": unique_sequence} for row in scenario.ground_truth],
                ground_truth_path=scenario.ground_truth_path,
                faults=scenario.faults,
                scoreable_roi=scenario.scoreable_roi,
            )
        run_sequence = f"run-{run_id[-8:]}-seq-{len(scenarios):02d}"
        scenario = Scenario(
            id=scenario.id,
            family=scenario.family,
            root=scenario.root,
            frames=[replace(frame, sequence_id=run_sequence) for frame in scenario.frames],
            ground_truth=[{**row, "sequence_id": run_sequence} for row in scenario.ground_truth],
            ground_truth_path=scenario.ground_truth_path,
            faults=scenario.faults,
            scoreable_roi=scenario.scoreable_roi,
        )
        scenarios.append(scenario)
    return scenarios


def _box_intersects_roi(box: list[float], roi: tuple[float, float, float, float]) -> bool:
    return min(box[2], roi[2]) > max(box[0], roi[0]) and min(box[3], roi[3]) > max(box[1], roi[1])


def _filter_outputs_to_scoreable_rois(
    collected: list[CollectedRecord], scenarios: list[Scenario]
) -> list[CollectedRecord]:
    rois = {scenario.frames[0].sequence_id: scenario.scoreable_roi for scenario in scenarios}
    filtered: list[CollectedRecord] = []
    for item in collected:
        roi = rois.get(item.system_record.get("sequence_id"))
        geometry = item.system_record.get("geometry", {})
        box = geometry.get("value") if isinstance(geometry, dict) else None
        if roi is None or not box or _box_intersects_roi(box, roi):
            filtered.append(item)
    return filtered


def run_benchmark(benchmark_path: str | Path, system_path: str | Path, output_root: str | Path) -> RunArtifacts:
    benchmark = load_benchmark(benchmark_path)
    system = load_system(system_path)
    run_id = _run_id()
    scenarios = _load_unique_scenarios(benchmark.scenarios, run_id, benchmark.evaluation_order_seed)
    run_dir = Path(output_root).resolve() / run_id
    run_dir.mkdir(parents=True)
    socket_dir = Path(tempfile.mkdtemp(prefix="cvb-", dir="/tmp"))
    socket_path = socket_dir / "input.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        _restrict_socket_access(socket_dir, socket_path)
        server.listen(1)
        server.settimeout(system.readiness_timeout_seconds)
    except OSError:
        server.close()
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise
    started_ns = time.monotonic_ns()
    started_wall = datetime.now(UTC).isoformat()
    outcome = RuntimeOutcome(status="failed")
    runtime: StartedRuntime | None = None
    startup_error: str | None = None
    monitor: ResourceMonitor | None = None
    collector: OutputCollector | None = None
    collected: list[CollectedRecord] = []
    collector_errors: list[str] = []
    stderr: list[str] = []
    ground_truth: list[dict[str, Any]] = []
    feed_counters = {
        "delivered_frames": 0,
        "dropped_frames": 0,
        "duplicate_frames": 0,
        "black_frames": 0,
        "feed_interruptions": 0,
        "delayed_frames": 0,
    }
    frame_delivery_ns: dict[tuple[str, int], int] = {}
    sequence_timestamps: dict[str, list[int]] = {}
    fault_events: dict[tuple[str, int], list[str]] = {}
    delivery = DeliveryRecorder(
        benchmark.delivery_policy,
        benchmark.replay_profile,
        benchmark.playback_rate,
    )
    ready_ns: int | None = None
    finished_ns: int | None = None
    teardown_finished_ns: int | None = None
    runtime_stopped = False
    run_deadline = time.monotonic() + benchmark.max_run_seconds
    try:
        runtime = start_runtime(system, socket_dir, run_dir)
        collector = OutputCollector(
            runtime.process,
            system.readiness_pattern,
            benchmark.max_output_records,
            benchmark.max_output_line_bytes,
            benchmark.max_total_output_bytes,
            benchmark.max_output_records_per_second,
            benchmark.thresholds.out_of_bounds,
        )
        collector.start()
        monitor = ResourceMonitor(
            runtime.process,
            cidfile=runtime.cidfile,
            cgroup_parent_name=runtime.accounting_cgroup_name,
            configured_cgroup_path=runtime.accounting_cgroup_path,
        )
        monitor.start()
        if system.runtime_type == "docker":
            verify_docker_isolation(runtime, socket_dir)
        readiness_budget = min(
            system.readiness_timeout_seconds,
            max(0.0, run_deadline - time.monotonic()),
        )
        if not _wait_for_readiness(collector, runtime, readiness_budget):
            exit_code = runtime.process.poll()
            outcome.exit_code = exit_code
            if collector.flooded.is_set():
                outcome.errors.append(f"output limit exceeded: {collector.limit_reason}")
            elif exit_code is None:
                outcome.errors.append("readiness timeout")
                outcome.timed_out = True
            else:
                outcome.errors.append("SUT exited before readiness")
                outcome.crashed = exit_code != 0
        else:
            ready_ns = time.monotonic_ns()
            outcome.startup_time_ms = (ready_ns - started_ns) / 1_000_000
            server.settimeout(max(0.001, run_deadline - time.monotonic()))
            connection, _ = server.accept()
            with connection:
                connection.settimeout(
                    max(0.001, min(2.0, run_deadline - time.monotonic()))
                )
                (
                    ground_truth,
                    feed_counters,
                    sequence_timestamps,
                    fault_events,
                    delivery,
                ) = _deliver_scenarios(
                    connection,
                    scenarios,
                    benchmark,
                    run_deadline,
                    monitor,
                    collector,
                    frame_delivery_ns,
                    delivery,
                )
                monitor.set_context(None, None, False, phase="drain")
                drain_budget = min(
                    system.grace_period_seconds,
                    benchmark.max_drain_seconds,
                    max(0.0, run_deadline - time.monotonic()),
                )
                stopped = stop_runtime(
                    runtime,
                    drain_budget,
                    monitor.capture_checkpoint,
                    lambda: _finish_scoring(monitor, collector),
                    lambda: _release_input(connection),
                    lambda: _scoring_complete(connection, collector),
                )
            runtime_stopped = True
            finished_ns = stopped.scoring_finished_ns
            teardown_finished_ns = stopped.teardown_finished_ns
            outcome.exit_code = stopped.exit_code
            outcome.timed_out = stopped.forced or stopped.scoring_timed_out
            if stopped.scoring_timed_out:
                outcome.errors.append(
                    "scoring drain deadline expired before stdout completion"
                )
            outcome.crashed = stopped.exit_code not in {0, None} and not outcome.timed_out
            outcome.status = (
                "completed" if stopped.exit_code == 0 and not outcome.timed_out else "failed"
            )
    except (OSError, RuntimeFailure, TimeoutError) as exc:
        startup_error = str(exc)
        outcome.errors.append(startup_error)
        outcome.timed_out = isinstance(exc, TimeoutError) or outcome.timed_out
        if runtime is not None:
            stopped = stop_runtime(
                runtime,
                0.1,
                monitor.capture_checkpoint if monitor is not None else None,
                (
                    (lambda: _finish_scoring(monitor, collector))
                    if monitor is not None and collector is not None
                    else None
                ),
            )
            runtime_stopped = True
            finished_ns = stopped.scoring_finished_ns
            teardown_finished_ns = stopped.teardown_finished_ns
            outcome.exit_code = stopped.exit_code
            outcome.crashed = (
                stopped.exit_code not in {0, None}
                and not stopped.forced
                and not outcome.timed_out
            )
    finally:
        server.close()
        if monitor is not None:
            monitor.stop()
        if collector is not None:
            collector.join()
            collected, collector_errors, stderr = collector.snapshot()
            if collector.first_output_timestamp_ns is not None:
                outcome.time_to_first_output_ms = (collector.first_output_timestamp_ns - started_ns) / 1_000_000
            if collector.flooded.is_set():
                outcome.status = "failed"
                message = f"output limit exceeded: {collector.limit_reason}"
                if message not in outcome.errors:
                    outcome.errors.append(message)
        if runtime is not None:
            if not runtime_stopped:
                stopped = stop_runtime(
                    runtime,
                    0,
                    monitor.capture_checkpoint if monitor is not None else None,
                    (
                        (lambda: _finish_scoring(monitor, collector))
                        if monitor is not None and collector is not None
                        else None
                    ),
                )
                finished_ns = finished_ns or stopped.scoring_finished_ns
            if finished_ns is None:
                finished_ns = time.monotonic_ns()
            outcome.resolved_image = runtime.resolved_image
            cleanup_errors = cleanup_runtime(
                runtime,
                (
                    monitor.accounting_cgroup_path
                    if monitor is not None and monitor.accounting_cgroup_path is not None
                    else runtime.accounting_cgroup_path
                ),
            )
            if cleanup_errors:
                outcome.status = "failed"
                outcome.errors.extend(cleanup_errors)
            teardown_finished_ns = time.monotonic_ns()
        shutil.rmtree(socket_dir, ignore_errors=True)

    if runtime is not None and system.runtime_type == "docker" and runtime.isolation.get("status") != "verified":
        outcome.status = "failed"
        verification_error = runtime.isolation.get("error", "Docker isolation verification failed")
        if verification_error not in outcome.errors:
            outcome.errors.append(str(verification_error))

    finished_ns = finished_ns or time.monotonic_ns()
    runtime_seconds = (finished_ns - started_ns) / 1_000_000_000
    if not ground_truth:
        # Preserve scoreable ground truth even for startup/crash failures.
        cursor = started_ns
        for scenario in scenarios:
            ground_truth.extend(_shift_ground_truth(scenario, cursor))
            cursor += scenario.frames[-1].relative_timestamp_ns + 100_000_000
    scenario_families = {scenario.frames[0].sequence_id: scenario.family for scenario in scenarios}
    scored_collected = _filter_outputs_to_scoreable_rois(collected, scenarios)
    metrics, matches = calculate_metrics(
        ground_truth,
        scored_collected,
        benchmark.thresholds,
        frame_delivery_ns=frame_delivery_ns,
        sequence_timestamps=sequence_timestamps or None,
        scenario_families=scenario_families,
        fault_timestamps=set(fault_events),
        fault_events=fault_events,
    )
    metrics["latency"]["authoritative"] = benchmark.input_mode == "online_replay"
    metrics["latency"]["mode"] = benchmark.input_mode
    resource_data = (
        monitor.summary(runtime_seconds)
        if monitor
        else unavailable_resource_summary(runtime_seconds, system.runtime_type)
    )
    timing_data = build_timing_summary(
        benchmark=benchmark,
        scenarios=scenarios,
        recorder=delivery,
        started_ns=started_ns,
        ready_ns=ready_ns,
        finished_ns=finished_ns,
        teardown_finished_ns=teardown_finished_ns,
        collected=collected,
        frame_delivery_ns=frame_delivery_ns,
    )
    feed_counters["input_queue_depth"] = timing_data["delivery"]["input_queue_depth"]
    feed_counters["input_queue_depth_available"] = timing_data["delivery"][
        "input_queue_depth_available"
    ]
    outcome.errors.extend(collector_errors)
    system_error_count = sum(
        item.system_record.get("event") == "system_error" for item in collected
    )
    if collector_errors or system_error_count:
        outcome.status = "failed"
    if system_error_count:
        outcome.errors.append(f"SUT emitted {system_error_count} system_error record(s)")
    leaderboard = build_leaderboard_semantics(
        benchmark=benchmark,
        timing=timing_data,
        resources=resource_data,
        metrics=metrics,
        outcome_status=outcome.status,
        runtime_type=system.runtime_type,
    )
    metrics["long_running_stability"] = evaluate_long_run_assertions(
        metrics["long_running_stability"], resource_data, benchmark.long_run_assertions
    )
    resources_csv = run_dir / "resources.csv"
    if monitor:
        monitor.write_csv(resources_csv)
    else:
        resources_csv.write_text("elapsed_ms,cpu_percent,memory_bytes\n")
    raw_output = run_dir / "system-output.jsonl"
    raw_output.write_text("".join(json.dumps(item.as_dict(), sort_keys=True) + "\n" for item in collected))
    (run_dir / "matching-decisions.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "sequence_id": match.sequence_id,
                    "source_timestamp_ns": match.source_timestamp_ns,
                    "target_id": match.target_id,
                    "track_id": match.track_id,
                    "iou": match.iou,
                    "center_error_px": match.center_error_px,
                    "support": match.output["support"],
                    "state": match.output["state"],
                },
                sort_keys=True,
            )
            + "\n"
            for match in matches
        )
    )
    (run_dir / "ground-truth.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in ground_truth))
    findings = generate_findings(metrics, asdict(outcome), resource_data, collector_errors)
    comparison_fingerprint, comparison_inputs = _comparison_fingerprint(
        benchmark,
        scenarios,
        system.resources,
        resource_data.get("accounting_availability"),
    )
    benchmark_display_path = _portable_path(benchmark.path)
    system_display_path = _portable_path(system.path)
    command = f"cvbench run --benchmark {benchmark_display_path} --system {system_display_path} --output <run-output>"
    report: dict[str, Any] = {
        "schema_version": "cvbench.report/v1",
        "run_id": run_dir.name,
        "started_at": started_wall,
        "mode": benchmark.input_mode,
        "benchmark": {"id": benchmark.id, "version": benchmark.version},
        "system": {
            "id": system.id,
            "revision": system.revision,
            "runtime": system.runtime_type,
            "command": list(system.command),
        },
        "outcome": asdict(outcome),
        "feed": feed_counters,
        "timing": timing_data,
        "metrics": metrics,
        "resources": resource_data,
        "leaderboard": leaderboard,
        "runtime_isolation": runtime.isolation
        if runtime
        else not_started_isolation(
            system,
            startup_error or "runtime did not start",
        ),
        "findings": findings,
        "comparison": [],
        "provenance": {
            "benchmark_path": benchmark_display_path,
            "benchmark_sha256": _sha256(benchmark.path),
            "system_path": system_display_path,
            "system_sha256": _sha256(system.path),
            "scenario_manifests": [_portable_path(path) for path in benchmark.scenarios],
            "resolved_container_image": outcome.resolved_image,
            "resolved_container_image_id": runtime.resolved_image_id if runtime else None,
            "executed_container_image_id": (
                runtime.isolation.get("image_identity", {}).get("executed_image_id") if runtime else None
            ),
            "command": command,
            "system_command": list(system.command),
            "matching": {
                "algorithm": "deterministic Hungarian assignment",
                "minimum_iou": benchmark.thresholds.minimum_match_iou,
                "maximum_center_error_px": benchmark.thresholds.max_match_center_error_px,
                "class_agnostic": benchmark.thresholds.class_agnostic,
                "ignore_match_iou": benchmark.thresholds.ignore_match_iou,
            },
            "external_clock": "time.monotonic_ns",
            "timing_compute_contract": benchmark.timing_compute_contract,
            "delivery_policy": benchmark.delivery_policy,
            "replay_profile": benchmark.replay_profile,
            "replay_rate": benchmark.playback_rate,
            "leaderboard_class": leaderboard["class_id"],
            "comparison_fingerprint": comparison_fingerprint,
            "comparison_inputs": comparison_inputs,
            "resource_envelope": comparison_inputs["resource_envelope"],
            "run_budgets": comparison_inputs["run_budgets"],
            "accounting_availability": comparison_inputs["accounting_availability"],
            "evaluation_order": {
                "scenario_ids": [scenario.id for scenario in scenarios],
                "mode": (
                    "configured_seed"
                    if benchmark.evaluation_order_seed is not None
                    else "private_per_run_fallback"
                ),
                "algorithm": EVALUATION_ORDER_ALGORITHM,
                "seed": benchmark.evaluation_order_seed,
                "private_per_run": benchmark.evaluation_order_seed is None,
                "run_scoped_sequence_ids": True,
                "public_calibration_note": (
                    "Scenario manifests and calibration clips are public and recognizable; this ordering and "
                    "the run-scoped sequence IDs do not provide secrecy."
                ),
            },
            "platform": {"os": os.name},
        },
        "diagnostics": {
            "collector_errors": collector_errors,
            "sut_stderr": stderr,
            "match_count": len(matches),
        },
        "limitations": [
            "GPU/VRAM metrics are omitted unless a future runner assigns an isolated device.",
            "Version 1 evidence overlays use synthetic source frames and MP4V when the local codec is available.",
            "Statistical comparison confidence is sample-count based; confidence intervals are not inferred.",
            "Portable Unix sockets do not expose queue depth; sender pressure and delivery "
            "backlog are reported instead.",
        ],
    }
    _, _, audit_unmatched = match_records_by_support(
        ground_truth,
        [item.system_record for item in scored_collected],
        benchmark.thresholds,
    )
    audit_timing_compute = {
        "contract_version": timing_data["contract_version"],
        "source": timing_data["source"],
        "replay": timing_data["replay"],
        "durations": timing_data["durations"],
        "delivery": {
            key: value
            for key, value in timing_data["delivery"].items()
            if key != "per_frame"
        },
        "processing_latency_ms": timing_data["processing_latency_ms"],
        "native_source_offset_ms": timing_data["native_source_offset_ms"],
        "output": timing_data["output"],
        "resources": {
            key: resource_data.get(key)
            for key in (
                "cpu_time_seconds",
                "cpu_seconds_per_native_source_second",
                "average_cpu_percent",
                "peak_cpu_percent",
                "peak_ram_bytes",
                "disk_read_bytes",
                "disk_write_bytes",
                "peak_process_count",
                "cpu_time_by_phase_seconds",
                "accounting_scope",
                "accounting_availability",
                "authoritative",
                "gpu_accounting",
            )
        },
        "leaderboard": leaderboard,
    }
    report["audit_evidence"] = build_audit_evidence(
        ground_truth,
        scored_collected,
        matches,
        metrics,
        feed_counters,
        resource_data,
        report["runtime_isolation"],
        neutral_outputs=[record for record in audit_unmatched if record.get("neutral_ignored")],
        timing_compute=audit_timing_compute,
    )
    report["provenance"]["raw_evidence_available"] = False
    report["provenance"]["bounded_audit_evidence_sha256"] = None
    report["provenance"]["bounded_audit_evidence_hash_algorithm"] = (
        "sha256(cvbench.canonical-json/v1); authoritative after Worker JSON parsing"
    )
    if benchmark.baseline_report:
        baseline = json.loads(benchmark.baseline_report.read_text())
        report["comparison"] = compare_reports(baseline, report)
    report_json, report_html = write_report_files(run_dir, report)
    if benchmark.reporting["generate_failure_packets"]:
        generate_evidence_packets(
            run_dir, findings, scenarios, ground_truth, collected, matches, resources_csv, command
        )
    return RunArtifacts(run_dir, report_json, report_html, raw_output, resources_csv)
