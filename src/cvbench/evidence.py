from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import cv2

from .model import CollectedRecord, Match, Scenario
from .reporting import write_json


def _video(
    path: Path,
    scenarios: list[Scenario],
    overlay: bool,
    ground_truth: list[dict[str, Any]],
    outputs: list[CollectedRecord],
) -> bool:
    frames = [(scenario, frame) for scenario in scenarios for frame in scenario.frames]
    if not frames:
        return False
    first = cv2.imread(str(frames[0][1].path))
    if first is None:
        return False
    height, width = first.shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (width, height))
    if not writer.isOpened():
        return False
    gt_by_relative: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in ground_truth:
        key = (row["sequence_id"], row.get("scenario_source_timestamp_ns", row["source_timestamp_ns"]))
        gt_by_relative.setdefault(key, []).append(row)
    output_by_sequence_frame: dict[tuple[str, int], list[dict[str, Any]]] = {}
    absolute_to_relative = {
        (row["sequence_id"], row["source_timestamp_ns"]): row.get(
            "scenario_source_timestamp_ns", row["source_timestamp_ns"]
        )
        for row in ground_truth
    }
    source_to_index: dict[tuple[str, int], int] = {}
    for scenario in scenarios:
        for frame in scenario.frames:
            source_to_index[(scenario.frames[0].sequence_id, frame.relative_timestamp_ns)] = frame.frame_index
    for item in outputs:
        record = item.system_record
        relative = absolute_to_relative.get((record["sequence_id"], record["source_timestamp_ns"]))
        if relative is None:
            continue
        frame_index = source_to_index.get((record["sequence_id"], relative))
        if frame_index is not None:
            output_by_sequence_frame.setdefault((record["sequence_id"], frame_index), []).append(record)
    try:
        for _scenario, frame in frames:
            image = cv2.imread(str(frame.path))
            if image is None:
                continue
            if overlay:
                for gt in gt_by_relative.get((frame.sequence_id, frame.relative_timestamp_ns), []):
                    if gt.get("bbox_xyxy"):
                        x1, y1, x2, y2 = map(int, gt["bbox_xyxy"])
                        cv2.rectangle(image, (x1, y1), (x2, y2), (255, 120, 0), 1)
                        cv2.putText(image, gt["target_id"], (x1, max(10, y1 - 2)), 0, 0.35, (255, 120, 0), 1)
                for record in output_by_sequence_frame.get((frame.sequence_id, frame.frame_index), []):
                    x1, y1, x2, y2 = map(int, record["geometry"]["value"])
                    cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 255), 1)
                    cv2.putText(image, record["track_id"], (x1, min(height - 2, y2 + 10)), 0, 0.35, (0, 255, 255), 1)
            writer.write(image)
    finally:
        writer.release()
    return True


def generate_evidence_packets(
    run_dir: Path,
    findings: list[dict[str, Any]],
    scenarios: list[Scenario],
    ground_truth: list[dict[str, Any]],
    outputs: list[CollectedRecord],
    matches: list[Match],
    resources_csv: Path,
    reproduction_command: str,
) -> None:
    packet_findings = [finding for finding in findings if finding["severity"] in {"high", "critical"}]
    if not packet_findings:
        return
    shared = run_dir / "failures" / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    shared_input = shared / "input_clip.mp4"
    shared_overlay = shared / "overlay.mp4"
    input_created = _video(shared_input, scenarios, False, ground_truth, outputs)
    overlay_created = _video(shared_overlay, scenarios, True, ground_truth, outputs)

    def attach(source: Path, destination: Path) -> None:
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    for finding in packet_findings:
        packet = run_dir / "failures" / finding["finding_id"]
        packet.mkdir(parents=True, exist_ok=True)
        write_json(packet / "finding.json", finding)
        (packet / "ground_truth.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in ground_truth)
        )
        (packet / "system_output.jsonl").write_text(
            "".join(json.dumps(item.as_dict(), sort_keys=True) + "\n" for item in outputs)
        )
        (packet / "matching-decisions.jsonl").write_text(
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
        write_json(
            packet / "timeline.json",
            {
                "input_timestamps_ns": sorted({row["source_timestamp_ns"] for row in ground_truth}),
                "outputs": [item.as_dict() for item in outputs],
                "matching_note": "See report.json metrics and deterministic matcher settings.",
            },
        )
        if resources_csv.exists():
            shutil.copy2(resources_csv, packet / "resources.csv")
        if input_created:
            attach(shared_input, packet / "input_clip.mp4")
        if overlay_created:
            attach(shared_overlay, packet / "overlay.mp4")
        (packet / "README.md").write_text(
            f"# {finding['finding_id']} evidence packet\n\n"
            f"{finding['interpretation']['statement']}\n\n"
            f"Reproduce with:\n\n```bash\n{reproduction_command}\n```\n\n"
            f"Input clip generated: {input_created}. Overlay generated: {overlay_created}.\n"
        )
