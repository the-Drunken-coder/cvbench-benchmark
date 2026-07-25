from __future__ import annotations

import json
import sys
from pathlib import Path


def _parse_mode(argv: list[str]) -> str:
    if len(argv) == 2:
        return "synthetic"
    if len(argv) == 3 and argv[2] in {"--combined", "--real-video"}:
        return argv[2].removeprefix("--")
    raise SystemExit("usage: assert_docker_report.py RUNS [--combined|--real-video]")


def main() -> None:
    mode = _parse_mode(sys.argv)
    reports = sorted(Path(sys.argv[1]).glob("*/report.json"))
    if len(reports) != 1:
        raise SystemExit(f"expected one Docker report, found {len(reports)}")
    report = json.loads(reports[0].read_text())
    isolation = report["runtime_isolation"]
    assert report["outcome"]["status"] == "completed", report["outcome"]
    assert report["metrics"]["sample_counts"]["matches"] > 0
    if mode == "real-video":
        assert report["benchmark"]["id"] == "real-video-full-frame-mot"
        assert report["benchmark"]["version"] == "2.0.0"
        assert report["metrics"]["multi_object_tracking"]["hota"] >= 0
        assert report["metrics"]["multi_object_tracking"]["idf1"] >= 0
        assert report["metrics"]["sample_counts"]["neutral_ignored_predictions"] == 0
    elif mode == "combined":
        assert report["benchmark"]["id"] == "public-whole-system-tracking"
        assert report["benchmark"]["version"] == "2.0.0"
        scenarios = report["provenance"]["comparison_inputs"]["scenarios"]
        expected_ids = {
            "synthetic-acquisition",
            "synthetic-false-detection",
            "synthetic-multi-target-identity",
            "synthetic-multi-target-pair",
            "synthetic-occlusion-gap-1000ms",
            "synthetic-occlusion-gap-100ms",
            "synthetic-occlusion-gap-2000ms",
            "synthetic-occlusion-gap-250ms",
            "synthetic-occlusion-gap-500ms",
            "synthetic-occlusion-reacquisition",
            "synthetic-resource-stress",
            "synthetic-track-id-churn",
            "synthetic-visible-retention",
            "rvmot-a1c9",
            "rvmot-b7e2",
            "rvmot-c4f6",
        }
        assert isinstance(scenarios, list)
        assert len(scenarios) == len(expected_ids)
        assert all(isinstance(scenario, dict) for scenario in scenarios)
        scenario_ids = [scenario.get("id") for scenario in scenarios]
        assert len(set(scenario_ids)) == len(scenario_ids)
        assert set(scenario_ids) == expected_ids
        assert report["metrics"]["multi_object_tracking"]["hota"] >= 0
    else:
        assert report["metrics"]["identity"]["id_switches"] == 0
    assert isolation["status"] == "verified", isolation
    assert isolation["future_frame_isolation"] is True
    assert isolation["ground_truth_access"] is False
    assert isolation["repository_access"] is False
    assert isolation["media_access"] is False
    assert isolation["image_identity_verified"] is True
    assert isolation["container_user_alignment_verified"] is True
    assert isolation["executed_container_user"] == isolation["expected_container_user"]
    uid, gid = (int(value) for value in isolation["expected_container_user"].split(":"))
    assert uid > 0 and gid >= 0
    assert isolation["socket_access"] == {
        "owner_uid": uid,
        "owner_gid": gid,
        "directory_mode": "0o700",
        "socket_mode": "0o600",
    }
    assert isolation["network_mode"] == "none"
    assert isolation["expected_mount"]["destination"] == "/run/cvbench"
    assert isolation["mounts"] == [isolation["expected_mount"]]
    assert isolation["applied"] == {"cpu_limit": 4.0, "memory_limit_mb": 2048.0}
    assert isolation["requested"] == {
        "cpu_limit": 4,
        "memory_limit_mb": 2048,
        "network_access": False,
    }
    assert report["resources"]["sample_count"] > 0
    assert report["resources"]["peak_process_count"] >= 1
    assert report["resources"]["accounting_scope"] == "container_cgroup_v2_external"
    assert report["resources"]["authoritative"] is True, {
        "accounting_availability": report["resources"]["accounting_availability"],
        "sample_count": report["resources"]["sample_count"],
        "cpu_time_seconds": report["resources"]["cpu_time_seconds"],
        "average_cpu_percent": report["resources"]["average_cpu_percent"],
        "peak_ram_bytes": report["resources"]["peak_ram_bytes"],
        "disk_read_bytes": report["resources"]["disk_read_bytes"],
        "disk_write_bytes": report["resources"]["disk_write_bytes"],
    }
    assert all(report["resources"]["accounting_availability"].values())
    assert report["resources"]["cpu_time_seconds"] is not None
    assert report["resources"]["cpu_seconds_per_native_source_second"] is not None
    assert report["resources"]["gpu_accounting"]["isolated"] is False
    assert report["timing"]["contract_version"] == "cvbench.timing-compute/v1"
    assert report["timing"]["source"]["immutable"] is True
    assert report["timing"]["replay"]["profile"] == "native"
    assert report["timing"]["replay"]["rate"] == 1
    assert report["timing"]["delivery"]["policy_version"] == "cvbench.delivery-lossless/v1"
    assert report["timing"]["durations"]["real_time_factor"] is not None
    assert report["leaderboard"]["policy_version"] == "cvbench.pareto/v1"
    assert report["leaderboard"]["replay_class"] == "native"
    assert report["leaderboard"]["composite_score"] is None
    assert report["leaderboard"]["eligible"] is True
    identity = isolation["image_identity"]
    assert identity["configured_reference"] != identity["resolved_reference"]
    assert identity["resolved_reference"] == report["provenance"]["resolved_container_image"]
    assert identity["resolved_image_id"] == report["provenance"]["resolved_container_image_id"]
    assert identity["executed_image_id"] == report["provenance"]["executed_container_image_id"]
    assert identity["executed_image_id"] == identity["resolved_image_id"]
    assert identity["executed_reference"] == identity["resolved_reference"]


if __name__ == "__main__":
    main()
