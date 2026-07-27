from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

WIDTH = 160
HEIGHT = 120
STEP_NS = 100_000_000
CHURN_RECENT_FRAME_WINDOW = 6
CHURN_RECENT_RADIUS = 45
CHURN_DORMANT_RADIUS = 12
CHURN_COLUMNS = 8
CHURN_ROWS = 6
CHURN_X_SPACING = 19
CHURN_Y_SPACING = 18


def _target(
    target_id: str,
    sequence_id: str,
    timestamp: int,
    box: list[int],
    *,
    eligible: bool = True,
    visibility: float = 1.0,
    occlusion: str = "none",
    **events: Any,
) -> dict[str, Any]:
    return {
        "target_id": target_id,
        "sequence_id": sequence_id,
        "source_timestamp_ns": timestamp,
        "on_screen": True,
        "eligible_for_detection": eligible,
        "visibility_fraction": visibility,
        "occlusion": occlusion,
        "class_id": "synthetic_target",
        "bbox_xyxy": box,
        **events,
    }


def _scenario_rows(family: str, index: int) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    sequence = f"synthetic_{index:02d}_{family}"
    frame_targets: list[list[dict[str, Any]]] = []
    faults: list[dict[str, Any]] = []
    for frame_index in range(12):
        timestamp = frame_index * STEP_NS
        rows: list[dict[str, Any]] = []
        if family == "acquisition":
            x1 = max(0, -12 + frame_index * 10)
            x2 = min(WIDTH, x1 + 20)
            rows.append(
                _target(
                    "gt_acquire",
                    sequence,
                    timestamp,
                    [x1, 40, x2, 66],
                    eligible=frame_index >= 2,
                    visibility=0.4 if frame_index < 2 else 1.0,
                    occlusion="partial" if frame_index < 2 else "none",
                    entry_event=frame_index == 0,
                )
            )
        elif family == "visible_retention":
            size = 16 + frame_index
            x1 = 10 + frame_index * 8
            rows.append(
                _target(
                    "gt_retain",
                    sequence,
                    timestamp,
                    [x1, 45 - frame_index // 3, x1 + size, 45 - frame_index // 3 + size],
                    entry_event=frame_index == 0,
                    exit_event=frame_index == 11,
                )
            )
        elif family == "occlusion_reacquisition":
            box = [20 + frame_index * 7, 45, 40 + frame_index * 7, 69]
            hidden = 4 <= frame_index <= 6
            rows.append(
                _target(
                    "gt_occluded",
                    sequence,
                    timestamp,
                    box,
                    eligible=not hidden,
                    visibility=0.0 if hidden else 1.0,
                    occlusion="full" if hidden else "none",
                    vision_loss_interval=hidden,
                    reappearance_event=frame_index == 7,
                    entry_event=frame_index == 0,
                )
            )
            if frame_index == 0:
                faults = [
                    {"type": "blackout", "frame_indices": [8]},
                    {"type": "frame_drop", "frame_indices": [10]},
                    {"type": "feed_interruption", "after_frame": 8, "duration_ms": 120},
                ]
        elif family == "multi_target_identity":
            for target_index in range(4):
                left = 8 + target_index * 36 + (frame_index * 3 if target_index % 2 == 0 else -frame_index * 2)
                top = 10 + target_index * 24
                rows.append(
                    _target(
                        f"gt_multi_{target_index}",
                        sequence,
                        timestamp,
                        [left, top, left + 14, top + 18],
                        entry_event=frame_index == 0,
                    )
                )
        elif family == "multi_target_pair":
            for target_index in range(2):
                left = 20 + target_index * 90 + (frame_index * 2 if target_index == 0 else -frame_index * 2)
                rows.append(
                    _target(
                        f"gt_pair_{target_index}",
                        sequence,
                        timestamp,
                        [left, 35 + target_index * 30, left + 16, 57 + target_index * 30],
                        entry_event=frame_index == 0,
                    )
                )
        elif family == "resource_stress":
            for target_index in range(8):
                column, row = target_index % 4, target_index // 4
                left = 6 + column * 38 + frame_index % 3
                top = 15 + row * 55
                rows.append(
                    _target(
                        f"gt_stress_{target_index}",
                        sequence,
                        timestamp,
                        [left, top, left + 12, top + 16],
                        entry_event=frame_index == 0,
                    )
                )
        frame_targets.append(rows)
    return frame_targets, faults


def _occlusion_gap_rows(family: str, index: int, gap_ms: int) -> list[list[dict[str, Any]]]:
    sequence = f"synthetic_{index:02d}_{family}"
    hidden_frames = gap_ms // 50
    rows_by_frame: list[list[dict[str, Any]]] = []
    for frame_index in range(hidden_frames + 5):
        timestamp = frame_index * 50_000_000
        hidden = 2 <= frame_index < hidden_frames + 2
        left = 20 + frame_index * 2
        rows_by_frame.append(
            [
                _target(
                    f"gt_gap_{gap_ms}",
                    sequence,
                    timestamp,
                    [left, 45, left + 18, 67],
                    eligible=not hidden,
                    visibility=0.0 if hidden else 1.0,
                    occlusion="full" if hidden else "none",
                    vision_loss_interval=hidden,
                    reappearance_event=frame_index == hidden_frames + 2,
                    entry_event=frame_index == 0,
                )
            ]
        )
    return rows_by_frame


def _track_id_churn_rows(family: str, index: int) -> list[list[dict[str, Any]]]:
    sequence = f"synthetic_{index:02d}_{family}"
    unused = [
        (5 + column * CHURN_X_SPACING, 5 + row * CHURN_Y_SPACING)
        for row in range(CHURN_ROWS)
        for column in range(CHURN_COLUMNS)
    ]
    births: list[tuple[int, tuple[int, int]]] = []
    rows_by_frame: list[list[dict[str, Any]]] = []
    while unused:
        frame_index = len(rows_by_frame)

        def eligible(point: tuple[int, int], current_frame: int = frame_index) -> bool:
            for prior_frame, prior in births:
                threshold = (
                    CHURN_RECENT_RADIUS
                    if current_frame - prior_frame <= CHURN_RECENT_FRAME_WINDOW
                    else CHURN_DORMANT_RADIUS
                )
                if (point[0] - prior[0]) ** 2 + (point[1] - prior[1]) ** 2 <= threshold**2:
                    return False
            return True

        candidates = [point for point in unused if eligible(point)]
        if not candidates:
            rows_by_frame.append([])
            continue
        point = max(
            candidates,
            key=lambda candidate: (
                min(
                    (
                        (candidate[0] - prior[0]) ** 2 + (candidate[1] - prior[1]) ** 2
                        for _, prior in births[-CHURN_RECENT_FRAME_WINDOW:]
                    ),
                    default=0,
                ),
                candidate,
            ),
        )
        birth = len(births)
        x, y = point
        rows_by_frame.append(
            [
                _target(
                    f"gt_churn_{birth:02d}",
                    sequence,
                    frame_index * STEP_NS,
                    [x, y, x + 10, y + 12],
                    entry_event=True,
                    exit_event=True,
                )
            ]
        )
        births.append((frame_index, point))
        unused.remove(point)
    return rows_by_frame


def generate_synthetic_pack(output: str | Path) -> list[Path]:
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifests: list[Path] = []
    families = [
        "acquisition",
        "visible_retention",
        "occlusion_reacquisition",
        "multi_target_identity",
        "false_detection",
        "resource_stress",
        "multi_target_pair",
        "occlusion_gap_100ms",
        "occlusion_gap_250ms",
        "occlusion_gap_500ms",
        "occlusion_gap_1000ms",
        "occlusion_gap_2000ms",
        "track_id_churn",
    ]
    for index, family in enumerate(families, 1):
        scenario_root = root / family
        frames_root = scenario_root / "frames"
        frames_root.mkdir(parents=True, exist_ok=True)
        if family.startswith("occlusion_gap_"):
            gap_ms = int(family.removeprefix("occlusion_gap_").removesuffix("ms"))
            rows_by_frame = _occlusion_gap_rows(family, index, gap_ms)
            faults: list[dict[str, Any]] = []
            step_ns = 50_000_000
        elif family == "track_id_churn":
            rows_by_frame = _track_id_churn_rows(family, index)
            faults = []
            step_ns = STEP_NS
        else:
            rows_by_frame, faults = _scenario_rows(family, index)
            step_ns = STEP_NS
        sequence = f"synthetic_{index:02d}_{family}"
        manifest_frames = []
        ground_truth: list[dict[str, Any]] = []
        for frame_index, rows in enumerate(rows_by_frame):
            image = np.full((HEIGHT, WIDTH, 3), (32, 36, 40), dtype=np.uint8)
            cv2.line(
                image, (0, (frame_index * 7) % HEIGHT), (WIDTH - 1, (frame_index * 7 + 25) % HEIGHT), (45, 45, 55), 2
            )
            if family == "false_detection":
                cv2.rectangle(image, (15 + frame_index * 4, 38), (33 + frame_index * 4, 62), (0, 0, 230), -1)
                cv2.circle(image, (120, 20 + frame_index * 5), 8, (220, 220, 220), -1)
            for row in rows:
                if row["visibility_fraction"] > 0:
                    x1, y1, x2, y2 = row["bbox_xyxy"]
                    cv2.rectangle(image, (x1, y1), (x2, y2), (20, 220, 20), -1)
                    if row["occlusion"] == "partial":
                        cv2.rectangle(image, (x1, y1), ((x1 + x2) // 2, y2), (50, 50, 50), -1)
            frame_path = frames_root / f"{frame_index:04d}.jpg"
            if not cv2.imwrite(str(frame_path), image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise OSError(f"could not write {frame_path}")
            manifest_frames.append(
                {
                    "frame_index": frame_index,
                    "source_timestamp_ns": frame_index * step_ns,
                    "width": WIDTH,
                    "height": HEIGHT,
                    "path": f"frames/{frame_index:04d}.jpg",
                }
            )
            ground_truth.extend(rows)
        (scenario_root / "ground_truth.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in ground_truth)
        )
        manifest = {
            "schema_version": "cvbench.scenario/v1",
            "id": f"synthetic-{family.replace('_', '-')}",
            "family": family,
            "sequence_id": sequence,
            "license": "CC0-1.0",
            "source": "Deterministically generated by cvbench.synthetic",
            "ground_truth": "ground_truth.jsonl",
            "frames": manifest_frames,
            "faults": faults,
        }
        manifest_path = scenario_root / "scenario.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        manifests.append(manifest_path)
    (root / "README.md").write_text(
        "# Synthetic Version 1 scenario pack\n\n"
        "These CC0 deterministic images cover acquisition, visible retention, exact occlusion-gap "
        "sweeps, 1/2/4/8-target identity and load, 48 distinct track births, false detections, and "
        "resource stress. Regenerate them with `cvbench scenarios generate scenarios/synthetic-v1`.\n"
    )
    return manifests
