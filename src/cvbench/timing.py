from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .config import PUBLIC_REPLAY_PROFILES, BenchmarkConfig
from .model import CollectedRecord, Scenario
from .protocol import TRACK_EVENTS

LEADERBOARD_POLICY_VERSION = "cvbench.pareto/v1"
SENDER_PRESSURE_THRESHOLD_NS = 5_000_000


def _seconds(nanoseconds: int | None) -> float | None:
    return nanoseconds / 1_000_000_000 if nanoseconds is not None else None


def _milliseconds(nanoseconds: int | None) -> float | None:
    return nanoseconds / 1_000_000 if nanoseconds is not None else None


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "sample_count": 0,
            "minimum": None,
            "median": None,
            "p95": None,
            "maximum": None,
        }
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "sample_count": len(ordered),
        "minimum": ordered[0],
        "median": percentile(0.5),
        "p95": percentile(0.95),
        "maximum": ordered[-1],
    }


def native_source_metadata(scenarios: list[Scenario]) -> dict[str, Any]:
    sequences: list[dict[str, Any]] = []
    total_duration_ns = 0
    total_frames = 0
    for scenario in scenarios:
        timestamps = [frame.relative_timestamp_ns for frame in scenario.frames]
        duration_ns = max(timestamps) - min(timestamps) if timestamps else 0
        intervals = [
            right - left
            for left, right in zip(timestamps, timestamps[1:], strict=False)
            if right > left
        ]
        cadence_ns = statistics.median(intervals) if intervals else None
        fps = 1_000_000_000 / cadence_ns if cadence_ns else None
        sequences.append(
            {
                "scenario_id": scenario.id,
                "sequence_id": scenario.frames[0].sequence_id,
                "frame_count": len(scenario.frames),
                "duration_seconds": _seconds(duration_ns),
                "nominal_fps": fps,
                "timestamp_origin": "scenario-relative nanoseconds",
            }
        )
        total_duration_ns += duration_ns
        total_frames += len(scenario.frames)
    return {
        "immutable": True,
        "frame_count": total_frames,
        "duration_seconds": _seconds(total_duration_ns),
        "sequences": sequences,
    }


def delivery_tolerance_ns(scenario: Scenario, replay_rate: float) -> int:
    timestamps = [frame.relative_timestamp_ns for frame in scenario.frames]
    intervals = [
        int((right - left) / replay_rate)
        for left, right in zip(timestamps, timestamps[1:], strict=False)
        if right > left
    ]
    return max(SENDER_PRESSURE_THRESHOLD_NS, int(statistics.median(intervals)) if intervals else 5_000_000)


@dataclass
class DeliveryRecorder:
    policy_version: str
    replay_profile: str
    replay_rate: float
    frames: list[dict[str, Any]] = field(default_factory=list)
    first_scheduled_ns: int | None = None
    first_send_started_ns: int | None = None
    last_send_completed_ns: int | None = None
    benchmark_end_send_started_ns: int | None = None
    benchmark_end_sent_ns: int | None = None

    def record_frame(
        self,
        *,
        sequence_id: str,
        frame_index: int,
        native_source_timestamp_ns: int,
        scheduled_ns: int,
        deadline_ns: int,
        send_started_ns: int,
        send_completed_ns: int,
        delivered: bool,
        drop_reason: str | None = None,
    ) -> None:
        self.first_scheduled_ns = (
            scheduled_ns
            if self.first_scheduled_ns is None
            else min(self.first_scheduled_ns, scheduled_ns)
        )
        self.first_send_started_ns = (
            send_started_ns
            if self.first_send_started_ns is None
            else min(self.first_send_started_ns, send_started_ns)
        )
        self.last_send_completed_ns = (
            send_completed_ns
            if self.last_send_completed_ns is None
            else max(self.last_send_completed_ns, send_completed_ns)
        )
        sender_call_ns = max(0, send_completed_ns - send_started_ns)
        backlog_ns = max(0, send_started_ns - scheduled_ns)
        self.frames.append(
            {
                "sequence_id": sequence_id,
                "frame_index": frame_index,
                "native_source_timestamp_ns": native_source_timestamp_ns,
                "scheduled_delivery_offset_ms": _milliseconds(
                    scheduled_ns - self.first_scheduled_ns
                ),
                "scoring_delivery_offset_ms": (
                    _milliseconds(send_completed_ns - self.first_scheduled_ns)
                    if delivered
                    else None
                ),
                "delivery_backlog_ms": _milliseconds(backlog_ns),
                "sender_call_ms": _milliseconds(sender_call_ns),
                "sender_pressure": sender_call_ns > SENDER_PRESSURE_THRESHOLD_NS,
                "deadline_missed": send_completed_ns > deadline_ns,
                "delivered": delivered,
                "drop_reason": drop_reason,
            }
        )

    def summary(self) -> dict[str, Any]:
        backlog = [float(frame["delivery_backlog_ms"]) for frame in self.frames]
        sender_calls = [float(frame["sender_call_ms"]) for frame in self.frames]
        pressure_calls = [
            float(frame["sender_call_ms"])
            for frame in self.frames
            if frame["sender_pressure"]
        ]
        delivered = sum(bool(frame["delivered"]) for frame in self.frames)
        return {
            "policy_version": self.policy_version,
            "semantics": "ordered lossless delivery; slow readers do not move the source clock",
            "replay_profile": self.replay_profile,
            "replay_rate": self.replay_rate,
            "frame_count": len(self.frames),
            "delivered_frames": delivered,
            "transport_failed_frames": sum(
                frame["drop_reason"] == "transport_failure" for frame in self.frames
            ),
            "policy_dropped_frames": sum(
                frame["drop_reason"] == "fault_injection" for frame in self.frames
            ),
            "deadline_missed_frames": sum(bool(frame["deadline_missed"]) for frame in self.frames),
            "sender_pressure_frames": len(pressure_calls),
            "sender_blocking_time_ms": sum(pressure_calls),
            "benchmark_end_sender_call_ms": (
                _milliseconds(
                    self.benchmark_end_sent_ns - self.benchmark_end_send_started_ns
                )
                if self.benchmark_end_sent_ns is not None
                and self.benchmark_end_send_started_ns is not None
                else None
            ),
            "delivery_backlog_ms": _summary(backlog),
            "sender_call_ms": _summary(sender_calls),
            "input_queue_depth": None,
            "input_queue_depth_available": False,
            "input_queue_depth_note": (
                "Unix stream queue depth is not portable; sender pressure and schedule backlog are "
                "the authoritative transport-pressure evidence."
            ),
            "per_frame": self.frames,
        }


def build_timing_summary(
    *,
    benchmark: BenchmarkConfig,
    scenarios: list[Scenario],
    recorder: DeliveryRecorder,
    started_ns: int,
    ready_ns: int | None,
    finished_ns: int,
    teardown_finished_ns: int | None,
    collected: list[CollectedRecord],
    frame_delivery_ns: dict[tuple[str, int], int],
) -> dict[str, Any]:
    source = native_source_metadata(scenarios)
    delivery = recorder.summary()
    processing_latencies = []
    native_source_offsets = []
    late_outputs = 0
    for item in collected:
        if (
            recorder.benchmark_end_sent_ns is not None
            and item.collector_received_timestamp_ns > recorder.benchmark_end_sent_ns
        ):
            late_outputs += 1
        if item.system_record.get("event") not in TRACK_EVENTS:
            continue
        key = (
            str(item.system_record.get("sequence_id")),
            int(item.system_record.get("source_timestamp_ns", -1)),
        )
        delivered_ns = frame_delivery_ns.get(key)
        if delivered_ns is not None:
            processing_latencies.append(
                max(0.0, (item.collector_received_timestamp_ns - delivered_ns) / 1_000_000)
            )
            native_source_offsets.append(
                (item.collector_received_timestamp_ns - key[1]) / 1_000_000
            )
    delivery_started_ns = recorder.first_send_started_ns
    delivery_finished_ns = recorder.benchmark_end_sent_ns or recorder.last_send_completed_ns
    native_duration = source["duration_seconds"]
    stream_delivery_seconds = (
        _seconds(delivery_finished_ns - delivery_started_ns)
        if delivery_started_ns is not None and delivery_finished_ns is not None
        else None
    )
    completion_seconds = (
        _seconds(finished_ns - delivery_started_ns)
        if delivery_started_ns is not None
        else None
    )
    delivery["effective_replay_rate"] = (
        native_duration / stream_delivery_seconds
        if native_duration and stream_delivery_seconds
        else None
    )
    delivery["delivered_frames_per_second"] = (
        delivery["delivered_frames"] / stream_delivery_seconds
        if stream_delivery_seconds
        else None
    )
    return {
        "contract_version": benchmark.timing_compute_contract,
        "source": source,
        "replay": {
            "profile": benchmark.replay_profile,
            "rate": benchmark.playback_rate,
            "native_real_time": benchmark.replay_profile == "native",
            "allowlisted": True,
        },
        "durations": {
            "wall_seconds": _seconds(finished_ns - started_ns),
            "runner_total_seconds": (
                _seconds(teardown_finished_ns - started_ns)
                if teardown_finished_ns is not None
                else None
            ),
            "teardown_seconds": (
                _seconds(teardown_finished_ns - finished_ns)
                if teardown_finished_ns is not None
                else None
            ),
            "startup_seconds": _seconds(ready_ns - started_ns) if ready_ns is not None else None,
            "stream_delivery_seconds": stream_delivery_seconds,
            "completion_seconds": completion_seconds,
            "drain_seconds": (
                _seconds(finished_ns - delivery_finished_ns)
                if delivery_finished_ns is not None
                else None
            ),
            "real_time_factor": (
                completion_seconds / native_duration
                if completion_seconds is not None and native_duration
                else None
            ),
        },
        "delivery": delivery,
        "processing_latency_ms": _summary(processing_latencies),
        "native_source_offset_ms": _summary(native_source_offsets),
        "output": {
            "records": len(collected),
            "records_per_native_source_second": (
                len(collected) / native_duration if native_duration else None
            ),
            "records_per_completion_second": (
                len(collected) / completion_seconds if completion_seconds else None
            ),
            "late_after_benchmark_end": late_outputs,
            "late_output_policy": (
                "accepted through the bounded drain window only for exact already-delivered source "
                "timestamps; retained latency remains externally measured"
            ),
        },
        "clocks": {
            "source": (
                "immutable native scenario time; source_timestamp_ns is the causal frame "
                "identity and is not the online latency origin"
            ),
            "delivery": (
                "independent monotonic replay schedule; successful frame-send completion "
                "is the online scoring origin"
            ),
            "completion": "external collector and runner monotonic time",
        },
    }


def _tier(value: float | None, limits: tuple[tuple[float, str], ...], overflow: str) -> str:
    if value is None:
        return "unclassified"
    for maximum, name in limits:
        if value <= maximum:
            return name
    return overflow


def build_leaderboard_semantics(
    *,
    benchmark: BenchmarkConfig,
    timing: dict[str, Any],
    resources: dict[str, Any],
    metrics: dict[str, Any],
    outcome_status: str,
    runtime_type: str,
) -> dict[str, Any]:
    native_duration = timing["source"]["duration_seconds"]
    cpu_time = resources.get("cpu_time_seconds")
    cpu_per_source = (
        float(cpu_time) / native_duration
        if isinstance(cpu_time, (int, float)) and native_duration
        else None
    )
    resources["cpu_seconds_per_native_source_second"] = cpu_per_source
    resources.setdefault(
        "accounting_scope",
        "container_cgroup_v2_external"
        if runtime_type == "docker"
        else "local_process_tree_best_effort",
    )
    resources.setdefault("authoritative", False)
    resources["gpu_accounting"] = {
        "available": False,
        "isolated": False,
        "authoritative": False,
        "note": "No isolated GPU device was assigned; host-wide GPU data is not reported.",
    }
    real_time_factor = timing["durations"]["real_time_factor"]
    cpu_tier = _tier(
        cpu_per_source,
        ((1.0, "cpu-1"), (2.0, "cpu-2"), (4.0, "cpu-4")),
        "cpu-over-4",
    )
    completion_tier = _tier(
        real_time_factor,
        ((1.05, "realtime"), (2.05, "completion-2x"), (4.05, "completion-4x")),
        "completion-over-4x",
    )
    replay_profile = benchmark.replay_profile
    class_id = f"{replay_profile}/{cpu_tier}/{completion_tier}"
    disqualifications = []
    if replay_profile not in PUBLIC_REPLAY_PROFILES:
        disqualifications.append("accelerated test replay is not a leaderboard pace")
    if runtime_type != "docker":
        disqualifications.append("resource accounting is not container/cgroup authoritative")
    availability = resources.get("accounting_availability")
    required_accounting = (
        "external_cgroup_v2",
        "final_cumulative_cpu_sample",
        "cpu_time",
        "cpu_percent",
        "peak_ram",
        "disk_io",
    )
    if (
        not resources.get("authoritative")
        or not isinstance(availability, dict)
        or not all(availability.get(key) is True for key in required_accounting)
    ):
        disqualifications.append("mandatory external cgroup accounting is incomplete")
    required_axes = {
        "CPU time": cpu_time,
        "CPU-seconds/native-source-second": cpu_per_source,
        "average CPU": resources.get("average_cpu_percent"),
        "peak CPU": resources.get("peak_cpu_percent"),
        "peak RAM": resources.get("peak_ram_bytes"),
        "disk read I/O": resources.get("disk_read_bytes"),
        "disk write I/O": resources.get("disk_write_bytes"),
        "real-time factor": real_time_factor,
    }
    missing_axes = [
        name for name, value in required_axes.items()
        if not isinstance(value, (int, float))
    ]
    if missing_axes:
        disqualifications.append(
            f"mandatory timing/compute axes are missing: {', '.join(missing_axes)}"
        )
    if outcome_status != "completed":
        disqualifications.append("run did not complete")
    return {
        "policy_version": LEADERBOARD_POLICY_VERSION,
        "ranking_method": "pareto",
        "composite_score": None,
        "class_id": class_id,
        "replay_class": replay_profile,
        "compute_tier": cpu_tier,
        "completion_tier": completion_tier,
        "eligible": not disqualifications,
        "disqualifications": disqualifications,
        "raw_axes": {
            "accuracy": {
                "acquisition_rate": metrics.get("acquisition", {}).get("rate"),
                "observed_coverage": metrics.get("coverage", {}).get("overall_observed"),
                "mean_iou": metrics.get("localization", {}).get("mean_iou"),
                "hota": metrics.get("multi_object_tracking", {}).get("hota"),
                "idf1": metrics.get("multi_object_tracking", {}).get("idf1"),
            },
            "efficiency": {
                "cpu_seconds_per_native_source_second": cpu_per_source,
                "real_time_factor": real_time_factor,
                "peak_ram_bytes": resources.get("peak_ram_bytes"),
                "disk_read_bytes": resources.get("disk_read_bytes"),
                "disk_write_bytes": resources.get("disk_write_bytes"),
            },
        },
        "comparison_rule": (
            "Compare only identical benchmark fingerprints and class IDs. A result Pareto-dominates "
            "another only when it is no worse on every declared raw axis and strictly better on at "
            "least one; raw accuracy and efficiency axes are always retained."
        ),
    }
