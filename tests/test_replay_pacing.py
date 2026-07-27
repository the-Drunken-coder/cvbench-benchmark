import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from cvbench.runner import run_benchmark

ROOT = Path(__file__).parents[1]


def _validate_report(report: dict) -> None:
    report_schema = json.loads((ROOT / "schemas/report-v1.schema.json").read_text())
    timing_schema = json.loads((ROOT / "schemas/timing-compute-v1.schema.json").read_text())
    registry = Registry().with_resource(
        "timing-compute-v1.schema.json",
        Resource.from_contents(timing_schema),
    )
    Draft202012Validator(report_schema, registry=registry).validate(report)


def _run(
    tmp_path: Path,
    profile: str,
    *,
    mode: str = "online_replay",
    immediate_output: bool = False,
    output_delay_ms: float = 0,
) -> dict:
    scenario_root = tmp_path / "scenario"
    scenario_root.mkdir(exist_ok=True)
    frames = []
    for index, timestamp in enumerate((0, 50_000_000, 100_000_000)):
        frame_path = scenario_root / f"{index}.jpg"
        frame_path.write_bytes(b"not-decoded-by-reader")
        frames.append(
            {
                "frame_index": index,
                "source_timestamp_ns": timestamp,
                "width": 10,
                "height": 10,
                "path": frame_path.name,
            }
        )
    (scenario_root / "ground_truth.jsonl").write_text("")
    manifest = scenario_root / "scenario.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.scenario/v1",
                "id": "pacing",
                "family": "pacing",
                "sequence_id": "pacing",
                "ground_truth": "ground_truth.jsonl",
                "frames": frames,
            }
        )
    )
    benchmark = tmp_path / f"{profile}-{mode}.yaml"
    benchmark.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.benchmark/v1",
                "id": "pacing",
                "version": "1",
                "input": {
                    "mode": mode,
                    "protocol": "frame_socket_v1",
                    "replay_profile": profile,
                },
                "scenarios": [str(manifest)],
                "reporting": {"generate_failure_packets": False},
                "thresholds": {"latency_deadline_ms": 30},
                "max_run_seconds": 3,
                "max_drain_seconds": 1,
            }
        )
    )
    system_dir = tmp_path / "systems"
    system_dir.mkdir(exist_ok=True)
    system = system_dir / "reader.yaml"
    system.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.system/v1",
                "id": "reader",
                "revision": "1",
                "runtime": {
                    "type": "local",
                    "command": [
                        sys.executable,
                        str(
                            ROOT
                            / "tests/fixtures"
                            / (
                                "sut_immediate_output.py"
                                if immediate_output
                                else "sut_reader.py"
                            )
                        ),
                    ],
                    "environment": {
                        "CVBENCH_TEST_OUTPUT_DELAY_MS": str(output_delay_ms)
                    },
                },
                "readiness": {"timeout_seconds": 1},
                "shutdown": {"grace_period_seconds": 1},
            }
        )
    )
    artifacts = run_benchmark(benchmark, system, tmp_path / f"runs-{profile}-{mode}")
    report = json.loads(artifacts.report_json.read_text())
    _validate_report(report)
    return report


def test_offline_debug_run_emits_a_schema_valid_report(tmp_path: Path) -> None:
    report = _run(tmp_path, "accelerated-test-20x", mode="offline_debug")
    assert report["mode"] == "offline_debug"
    assert report["provenance"]["comparison_inputs"]["input_mode"] == "offline_debug"


def test_native_and_slower_replay_keep_identical_source_truth_but_separate_delivery_class(
    tmp_path: Path,
) -> None:
    native = _run(tmp_path, "native")
    half = _run(tmp_path, "half-speed")

    assert native["timing"]["source"]["duration_seconds"] == 0.1
    assert half["timing"]["source"]["duration_seconds"] == 0.1
    native_frames = native["timing"]["delivery"]["per_frame"]
    half_frames = half["timing"]["delivery"]["per_frame"]
    assert [frame["native_source_timestamp_ns"] for frame in native_frames] == [
        0,
        50_000_000,
        100_000_000,
    ]
    assert [frame["native_source_timestamp_ns"] for frame in half_frames] == [
        0,
        50_000_000,
        100_000_000,
    ]
    assert native_frames[-1]["scheduled_delivery_offset_ms"] == 100
    assert half_frames[-1]["scheduled_delivery_offset_ms"] == 200
    native_rate = native["timing"]["delivery"]["effective_replay_rate"]
    half_rate = half["timing"]["delivery"]["effective_replay_rate"]
    assert native_rate == pytest.approx(1, rel=0.35)
    assert half_rate == pytest.approx(native_rate * 0.5, rel=0.25)
    assert native["provenance"]["comparison_fingerprint"] != half["provenance"][
        "comparison_fingerprint"
    ]
    assert native["leaderboard"]["replay_class"] == "native"
    assert half["leaderboard"]["replay_class"] == "half-speed"


def test_immediate_output_latency_uses_delivery_clock_at_every_allowed_slow_rate(
    tmp_path: Path,
) -> None:
    reports = {
        profile: _run(tmp_path, profile, immediate_output=True)
        for profile in ("native", "half-speed", "quarter-speed")
    }
    delivery_latencies = [
        report["metrics"]["latency"]["median"] for report in reports.values()
    ]

    assert max(delivery_latencies) - min(delivery_latencies) < 20
    for report in reports.values():
        assert report["metrics"]["latency"]["clock"] == (
            "successful_frame_delivery_completion"
        )
        assert report["metrics"]["latency"]["deadline_miss_rate"] == 0
        assert report["timing"]["processing_latency_ms"]["median"] == pytest.approx(
            report["metrics"]["latency"]["median"], abs=5
        )
        assert all(
            frame["scoring_delivery_offset_ms"] is not None
            for frame in report["timing"]["delivery"]["per_frame"]
        )

    assert (
        reports["native"]["timing"]["native_source_offset_ms"]["p95"]
        < reports["half-speed"]["timing"]["native_source_offset_ms"]["p95"]
        < reports["quarter-speed"]["timing"]["native_source_offset_ms"]["p95"]
    )


def test_real_system_delay_still_counts_from_delivery_and_misses_deadline(
    tmp_path: Path,
) -> None:
    report = _run(
        tmp_path,
        "quarter-speed",
        immediate_output=True,
        output_delay_ms=60,
    )

    assert report["metrics"]["latency"]["median"] == pytest.approx(60, abs=20)
    assert report["metrics"]["latency"]["deadline_miss_rate"] == 1
    assert report["timing"]["processing_latency_ms"]["median"] == pytest.approx(
        60, abs=20
    )
    assert (
        report["timing"]["native_source_offset_ms"]["median"]
        > report["timing"]["processing_latency_ms"]["median"]
    )


def test_slow_reader_creates_reported_sender_pressure_without_rewriting_source_time(
    tmp_path: Path,
) -> None:
    scenario_root = tmp_path / "slow-scenario"
    scenario_root.mkdir()
    frames = []
    for index in range(12):
        frame_path = scenario_root / f"{index}.jpg"
        frame_path.write_bytes(bytes([index]) * 256_000)
        frames.append(
            {
                "frame_index": index,
                "source_timestamp_ns": index * 10_000_000,
                "width": 10,
                "height": 10,
                "path": frame_path.name,
            }
        )
    (scenario_root / "ground_truth.jsonl").write_text("")
    manifest = scenario_root / "scenario.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.scenario/v1",
                "id": "slow-reader",
                "family": "pacing",
                "sequence_id": "slow-reader",
                "ground_truth": "ground_truth.jsonl",
                "frames": frames,
            }
        )
    )
    benchmark = tmp_path / "slow.yaml"
    benchmark.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.benchmark/v1",
                "id": "slow-reader",
                "version": "1",
                "input": {
                    "mode": "online_replay",
                    "protocol": "frame_socket_v1",
                    "replay_profile": "accelerated-test-100x",
                },
                "scenarios": [str(manifest)],
                "reporting": {"generate_failure_packets": False},
                "max_run_seconds": 5,
            }
        )
    )
    system_dir = tmp_path / "systems"
    system_dir.mkdir()
    system = system_dir / "slow.yaml"
    system.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.system/v1",
                "id": "slow-reader",
                "revision": "1",
                "runtime": {
                    "type": "local",
                    "command": [
                        sys.executable,
                        str(ROOT / "tests/fixtures/sut_slow_reader.py"),
                    ],
                },
                "readiness": {"timeout_seconds": 1},
                "shutdown": {"grace_period_seconds": 1},
            }
        )
    )
    artifacts = run_benchmark(benchmark, system, tmp_path / "slow-runs")
    report = json.loads(artifacts.report_json.read_text())
    delivery = report["timing"]["delivery"]
    assert report["outcome"]["status"] == "completed"
    assert report["timing"]["source"]["duration_seconds"] == 0.11
    assert delivery["sender_pressure_frames"] > 0
    assert delivery["delivery_backlog_ms"]["maximum"] > 0
    assert delivery["deadline_missed_frames"] > 0
    assert delivery["input_queue_depth_available"] is False
    assert delivery["transport_failed_frames"] == 0
    assert delivery["benchmark_end_sender_call_ms"] is not None
    assert delivery["benchmark_end_sender_call_ms"] >= 0
