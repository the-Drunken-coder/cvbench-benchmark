from pathlib import Path

import pytest

from cvbench.config import load_benchmark
from cvbench.model import Frame, Scenario
from cvbench.runner import _shift_ground_truth
from cvbench.timing import (
    DeliveryRecorder,
    build_leaderboard_semantics,
    native_source_metadata,
)

ROOT = Path(__file__).parents[1]


def _scenario() -> Scenario:
    timestamps = [0, 33_333_333, 66_666_667, 100_000_000]
    frames = [
        Frame("native-30", index, timestamp, 10, 10, Path(f"{index}.jpg"))
        for index, timestamp in enumerate(timestamps)
    ]
    truth = [
        {
            "sequence_id": "native-30",
            "target_id": "target",
            "source_timestamp_ns": timestamp,
        }
        for timestamp in timestamps
    ]
    return Scenario("native-30", "test", Path("."), frames, truth)


def test_native_source_metadata_is_immutable_and_independent_of_replay_rate() -> None:
    scenario = _scenario()
    metadata = native_source_metadata([scenario])
    assert metadata["immutable"] is True
    assert metadata["duration_seconds"] == 0.1
    assert metadata["sequences"][0]["nominal_fps"] == pytest.approx(30, rel=1e-6)

    shifted = _shift_ground_truth(scenario, 1_000_000_000)
    assert [
        row["source_timestamp_ns"] - 1_000_000_000 for row in shifted
    ] == [frame.relative_timestamp_ns for frame in scenario.frames]
    assert [
        row["scenario_source_timestamp_ns"] for row in shifted
    ] == [frame.relative_timestamp_ns for frame in scenario.frames]


def test_delivery_policy_reports_backlog_sender_pressure_and_deadline_miss() -> None:
    recorder = DeliveryRecorder("cvbench.delivery-lossless/v1", "half-speed", 0.5)
    recorder.record_frame(
        sequence_id="sequence",
        frame_index=0,
        native_source_timestamp_ns=0,
        scheduled_ns=1_000_000_000,
        deadline_ns=1_010_000_000,
        send_started_ns=1_020_000_000,
        send_completed_ns=1_030_000_000,
        delivered=True,
    )
    summary = recorder.summary()
    assert summary["deadline_missed_frames"] == 1
    assert summary["sender_pressure_frames"] == 1
    assert summary["sender_blocking_time_ms"] == 10
    assert summary["delivery_backlog_ms"]["maximum"] == 20
    assert summary["policy_dropped_frames"] == 0
    assert summary["per_frame"][0]["scoring_delivery_offset_ms"] == 30


def test_sleeping_cannot_improve_both_efficiency_axes_or_share_a_class() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    metrics = {
        "acquisition": {"rate": 1},
        "coverage": {"overall_observed": 1},
        "localization": {"mean_iou": 1},
        "multi_object_tracking": {"hota": 1, "idf1": 1},
    }
    cpu_heavy_resources = {"cpu_time_seconds": 30.0}
    cpu_heavy = build_leaderboard_semantics(
        benchmark=benchmark,
        timing={
            "source": {"duration_seconds": 10.0},
            "durations": {"real_time_factor": 1.0},
        },
        resources=cpu_heavy_resources,
        metrics=metrics,
        outcome_status="completed",
        runtime_type="docker",
    )
    idle_resources = {"cpu_time_seconds": 1.0}
    idle = build_leaderboard_semantics(
        benchmark=benchmark,
        timing={
            "source": {"duration_seconds": 10.0},
            "durations": {"real_time_factor": 3.0},
        },
        resources=idle_resources,
        metrics=metrics,
        outcome_status="completed",
        runtime_type="docker",
    )

    assert cpu_heavy_resources["cpu_seconds_per_native_source_second"] == 3
    assert idle_resources["cpu_seconds_per_native_source_second"] == 0.1
    assert cpu_heavy["class_id"] == "native/cpu-4/realtime"
    assert idle["class_id"] == "native/cpu-1/completion-4x"
    assert cpu_heavy["class_id"] != idle["class_id"]
    assert cpu_heavy["composite_score"] is None
    assert idle["composite_score"] is None


def test_local_process_tree_measurements_are_never_leaderboard_authoritative() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    resources = {"cpu_time_seconds": 1.0}
    semantics = build_leaderboard_semantics(
        benchmark=benchmark,
        timing={
            "source": {"duration_seconds": 10.0},
            "durations": {"real_time_factor": 1.0},
        },
        resources=resources,
        metrics={},
        outcome_status="completed",
        runtime_type="local",
    )
    assert resources["accounting_scope"] == "local_process_tree_best_effort"
    assert resources["authoritative"] is False
    assert semantics["eligible"] is False
    assert "container/cgroup" in semantics["disqualifications"][0]


def test_missing_mandatory_compute_axes_make_docker_result_ineligible() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    resources = {
        "cpu_time_seconds": None,
        "average_cpu_percent": None,
        "peak_cpu_percent": None,
        "peak_ram_bytes": None,
        "disk_read_bytes": None,
        "disk_write_bytes": None,
        "authoritative": False,
        "accounting_availability": {},
    }
    semantics = build_leaderboard_semantics(
        benchmark=benchmark,
        timing={
            "source": {"duration_seconds": 10.0},
            "durations": {"real_time_factor": None},
        },
        resources=resources,
        metrics={},
        outcome_status="completed",
        runtime_type="docker",
    )
    assert semantics["eligible"] is False
    assert semantics["compute_tier"] == "unclassified"
    assert semantics["completion_tier"] == "unclassified"
    assert any("mandatory timing/compute axes" in reason for reason in semantics["disqualifications"])
