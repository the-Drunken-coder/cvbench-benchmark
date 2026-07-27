import json
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

from cvbench.runner import run_benchmark


def test_good_tracker_decodes_novel_shifted_images(tmp_path: Path) -> None:
    scenario_root = tmp_path / "novel-shifted"
    frames_root = scenario_root / "frames"
    frames_root.mkdir(parents=True)
    frames = []
    truth = []
    sequence = "unseen-seed-941"
    for index in range(6):
        image = np.full((120, 160, 3), (17 + index, 23, 31), dtype=np.uint8)
        box = [73 + index * 5, 61 - index, 96 + index * 5, 89 - index]
        cv2.rectangle(image, (box[0], box[1]), (box[2], box[3]), (20, 220, 20), -1)
        path = frames_root / f"seed941-{index}.jpg"
        assert cv2.imwrite(str(path), image)
        timestamp = index * 80_000_000
        frames.append(
            {
                "frame_index": 700 + index,
                "source_timestamp_ns": timestamp,
                "width": 160,
                "height": 120,
                "path": f"frames/{path.name}",
            }
        )
        truth.append(
            {
                "target_id": "novel-green-object",
                "sequence_id": sequence,
                "source_timestamp_ns": timestamp,
                "on_screen": True,
                "eligible_for_detection": True,
                "visibility_fraction": 1.0,
                "occlusion": "none",
                "class_id": "synthetic_target",
                "bbox_xyxy": box,
            }
        )
    (scenario_root / "ground_truth.jsonl").write_text("".join(json.dumps(row) + "\n" for row in truth))
    manifest = scenario_root / "scenario.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.scenario/v1",
                "id": "novel-shifted-seed",
                "family": "acquisition",
                "sequence_id": sequence,
                "ground_truth": "ground_truth.jsonl",
                "frames": frames,
                "faults": [
                    {"type": "duplicate", "frame_indices": [701]},
                    {"type": "delay", "frame_indices": [702], "duration_ms": 1},
                ],
            }
        )
    )
    benchmark = tmp_path / "benchmark.yaml"
    benchmark.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.benchmark/v1",
                "id": "novel-image-e2e",
                "version": "1",
                "input": {
                    "mode": "online_replay",
                    "protocol": "frame_socket_v1",
                    "replay_profile": "accelerated-test-20x",
                },
                "thresholds": {"minimum_match_iou": 0.3, "max_match_center_error_px": 20},
                "scenarios": [str(manifest)],
                "reporting": {"generate_failure_packets": False},
                "max_run_seconds": 5,
            }
        )
    )
    systems = tmp_path / "systems"
    systems.mkdir()
    system = systems / "system.yaml"
    system.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.system/v1",
                "id": "good-novel-image-test",
                "revision": "unseen-seed",
                "runtime": {
                    "type": "local",
                    "command": [sys.executable, "-m", "cvbench.examples.good_tracker"],
                },
                "readiness": {"type": "stdout_pattern", "pattern": "CVBENCH_READY", "timeout_seconds": 2},
                "shutdown": {"grace_period_seconds": 1},
            }
        )
    )
    artifacts = run_benchmark(benchmark, system, tmp_path / "runs")
    report = json.loads(artifacts.report_json.read_text())
    assert report["outcome"]["status"] == "completed"
    assert report["metrics"]["acquisition"]["rate"] == 1
    assert report["metrics"]["localization"]["mean_iou"] > 0.75
    assert report["metrics"]["sample_counts"]["matches"] == 6
    assert report["feed"]["duplicate_frames"] == 1
    assert report["feed"]["delayed_frames"] == 1
    assert report["provenance"]["raw_evidence_available"] is False
    assert report["provenance"]["bounded_audit_evidence_sha256"] is None
    assert "authoritative after Worker JSON parsing" in report["provenance"]["bounded_audit_evidence_hash_algorithm"]
