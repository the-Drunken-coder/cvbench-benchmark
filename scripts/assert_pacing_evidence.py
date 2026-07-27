#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _report(root: Path) -> dict[str, Any]:
    reports = sorted(root.glob("*/report.json"))
    if len(reports) != 1:
        raise ValueError(f"expected one report below {root}, found {len(reports)}")
    return json.loads(reports[0].read_text())


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "system": report["system"]["id"],
        "native_source_duration_seconds": report["timing"]["source"]["duration_seconds"],
        "replay_profile": report["timing"]["replay"]["profile"],
        "replay_rate": report["timing"]["replay"]["rate"],
        "stream_delivery_seconds": report["timing"]["durations"]["stream_delivery_seconds"],
        "effective_replay_rate": report["timing"]["delivery"]["effective_replay_rate"],
        "delivered_frames_per_second": report["timing"]["delivery"][
            "delivered_frames_per_second"
        ],
        "completion_seconds": report["timing"]["durations"]["completion_seconds"],
        "real_time_factor": report["timing"]["durations"]["real_time_factor"],
        "cpu_time_seconds": report["resources"]["cpu_time_seconds"],
        "cpu_seconds_per_native_source_second": report["resources"][
            "cpu_seconds_per_native_source_second"
        ],
        "average_cpu_percent": report["resources"]["average_cpu_percent"],
        "peak_cpu_percent": report["resources"]["peak_cpu_percent"],
        "peak_ram_bytes": report["resources"]["peak_ram_bytes"],
        "disk_read_bytes": report["resources"]["disk_read_bytes"],
        "disk_write_bytes": report["resources"]["disk_write_bytes"],
        "peak_process_count": report["resources"]["peak_process_count"],
        "output_records_per_native_source_second": report["timing"]["output"][
            "records_per_native_source_second"
        ],
        "output_records_per_completion_second": report["timing"]["output"][
            "records_per_completion_second"
        ],
        "processing_latency_p95_ms": report["timing"]["processing_latency_ms"]["p95"],
        "native_source_offset_p95_ms": report["timing"]["native_source_offset_ms"]["p95"],
        "delivery_deadline_missed_frames": report["timing"]["delivery"][
            "deadline_missed_frames"
        ],
        "delivery_backlog_max_ms": report["timing"]["delivery"]["delivery_backlog_ms"][
            "maximum"
        ],
        "acquisition_rate": report["metrics"]["acquisition"]["rate"],
        "observed_coverage": report["metrics"]["coverage"]["overall_observed"],
        "leaderboard_class": report["leaderboard"]["class_id"],
    }


def _efficiency_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    axes = (
        ("resources", "cpu_seconds_per_native_source_second"),
        ("timing", "durations", "real_time_factor"),
        ("resources", "peak_ram_bytes"),
        ("resources", "disk_read_bytes"),
        ("resources", "disk_write_bytes"),
    )

    def value(report: dict[str, Any], path: tuple[str, ...]) -> float:
        current: Any = report
        for part in path:
            current = current[part]
        if not isinstance(current, (int, float)):
            raise AssertionError(f"missing Pareto axis: {'.'.join(path)}")
        return float(current)

    pairs = [(value(left, path), value(right, path)) for path in axes]
    return all(candidate <= baseline for candidate, baseline in pairs) and any(
        candidate < baseline for candidate, baseline in pairs
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", type=Path, required=True)
    parser.add_argument("--cpu-heavy", type=Path, required=True)
    parser.add_argument("--idle", type=Path, required=True)
    parser.add_argument("--background-child", type=Path, required=True)
    parser.add_argument("--final-burst", type=Path, required=True)
    parser.add_argument("--immediate-exit", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = {
        "fast": _report(args.fast),
        "cpu_heavy": _report(args.cpu_heavy),
        "idle": _report(args.idle),
        "background_child": _report(args.background_child),
        "final_burst": _report(args.final_burst),
        "immediate_exit": _report(args.immediate_exit),
    }
    for report in reports.values():
        assert report["outcome"]["status"] == "completed", report["outcome"]
        assert report["runtime_isolation"]["status"] == "verified"
        assert report["resources"]["accounting_scope"] == "container_cgroup_v2_external"
        assert report["resources"]["authoritative"] is True
        assert all(report["resources"]["accounting_availability"].values())
        assert report["resources"]["cpu_time_seconds"] is not None
        assert report["resources"]["cpu_seconds_per_native_source_second"] is not None
        assert report["timing"]["replay"] == {
            "profile": "native",
            "rate": 1.0,
            "native_real_time": True,
            "allowlisted": True,
        }
        assert report["timing"]["source"]["duration_seconds"] == 1.1

    fast = reports["fast"]
    cpu_heavy = reports["cpu_heavy"]
    idle = reports["idle"]
    child = reports["background_child"]
    final_burst = reports["final_burst"]
    immediate_exit = reports["immediate_exit"]
    assert cpu_heavy["timing"]["durations"]["real_time_factor"] > fast["timing"]["durations"]["real_time_factor"]
    assert idle["timing"]["durations"]["real_time_factor"] > fast["timing"]["durations"]["real_time_factor"]
    assert (
        idle["resources"]["cpu_seconds_per_native_source_second"]
        >= fast["resources"]["cpu_seconds_per_native_source_second"] * 0.8
    )
    assert (
        cpu_heavy["resources"]["cpu_seconds_per_native_source_second"]
        > idle["resources"]["cpu_seconds_per_native_source_second"]
    )
    assert child["resources"]["peak_process_count"] >= 2
    assert (
        child["resources"]["cpu_seconds_per_native_source_second"]
        > fast["resources"]["cpu_seconds_per_native_source_second"]
    )
    burst_samples = final_burst["resources"]["over_time"]
    first_drain = next(sample for sample in burst_samples if sample["phase"] == "drain")
    final_sample = burst_samples[-1]
    assert final_sample["final_cumulative"] is True
    assert final_sample["cpu_time_seconds"] > first_drain["cpu_time_seconds"]
    assert final_sample["disk_write_bytes"] > first_drain["disk_write_bytes"]
    assert immediate_exit["resources"]["over_time"][-1]["final_cumulative"] is True
    assert immediate_exit["metrics"]["sample_counts"]["output_records"] > 0
    assert immediate_exit["resources"]["cpu_time_seconds"] > 0
    assert immediate_exit["resources"]["peak_ram_bytes"] > 0
    assert immediate_exit["resources"]["disk_read_bytes"] >= 0
    assert immediate_exit["resources"]["disk_write_bytes"] >= 0
    accuracy = {
        (
            report["metrics"]["acquisition"]["rate"],
            report["metrics"]["coverage"]["overall_observed"],
        )
        for report in reports.values()
    }
    assert len(accuracy) == 1
    assert len({report["leaderboard"]["class_id"] for report in reports.values()}) >= 3
    tactic_dominates_fast = {
        name: _efficiency_dominates(report, fast)
        for name, report in reports.items()
        if name not in {"fast", "immediate_exit"}
    }
    assert tactic_dominates_fast == {
        "cpu_heavy": False,
        "idle": False,
        "background_child": False,
        "final_burst": False,
    }

    evidence = {
        "schema_version": "cvbench.timing-compute-evidence/v1",
        "benchmark": {"id": "timing-compute-evidence", "version": "1.0.0"},
        "contract_version": "cvbench.timing-compute/v1",
        "delivery_policy": "cvbench.delivery-lossless/v1",
        "leaderboard_policy": "cvbench.pareto/v1",
        "runs": {name: _summary(report) for name, report in reports.items()},
        "pareto": {"tactic_dominates_fast": tactic_dominates_fast},
        "conclusion": (
            "Sleeping does not erase the system's required CPU work and increases completion "
            "time; CPU-heavy work consumes CPU-seconds per native source-second; background "
            "child work remains in cgroup CPU and process accounting. No tactic improves every "
            "raw efficiency axis, and accuracy is retained separately."
        ),
    }
    payload = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
