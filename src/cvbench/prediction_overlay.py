from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .model import CollectedRecord, Scenario
from .protocol import SUPPORT_VALUES, TRACK_OBSERVATION_EVENTS, TRACK_STATES

SCHEMA_VERSION = "cvbench.prediction-overlay/v1"
PUBLIC_CLASSES = {"synthetic_target", "person", "vehicle", "dog"}
MAX_PREDICTIONS_PER_FRAME = 128
MAX_PREDICTIONS_PER_SCENARIO = 8192
MAX_TRACKS_PER_SCENARIO = 4096
MAX_SCENARIO_BYTES = 1536 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
SAFE_CLASS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")


def write_prediction_overlays(
    output_dir: Path,
    scenarios: list[Scenario],
    collected: list[CollectedRecord],
    sequence_timestamps: dict[str, list[int]],
) -> list[Path]:
    """Write a bounded visualization projection without raw model identifiers."""
    output_dir.mkdir(parents=True, exist_ok=True)
    records_by_sequence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in collected:
        record = item.system_record
        if record.get("event") in TRACK_OBSERVATION_EVENTS:
            records_by_sequence[str(record.get("sequence_id", ""))].append(record)

    prepared: list[tuple[Scenario, dict[str, Any], bytes]] = []
    for scenario in scenarios:
        payload = _scenario_overlay(
            scenario,
            records_by_sequence.get(scenario.frames[0].sequence_id, []),
            sequence_timestamps.get(scenario.frames[0].sequence_id, []),
        )
        encoded = _encoded(payload)
        if len(encoded) > MAX_SCENARIO_BYTES:
            payload = _unavailable_overlay(scenario, "budget_exceeded")
            encoded = _encoded(payload)
        prepared.append((scenario, payload, encoded))

    while sum(len(encoded) for _, _, encoded in prepared) > MAX_TOTAL_BYTES:
        candidates = [
            (len(encoded), index)
            for index, (_, payload, encoded) in enumerate(prepared)
            if payload["state"] == "complete"
        ]
        if not candidates:
            break
        _, index = max(candidates)
        scenario = prepared[index][0]
        payload = _unavailable_overlay(scenario, "budget_exceeded")
        prepared[index] = (scenario, payload, _encoded(payload))

    paths: list[Path] = []
    for scenario, _payload, encoded in prepared:
        path = output_dir / f"{scenario.id}.json"
        path.write_bytes(encoded)
        paths.append(path)
    return paths


def _scenario_overlay(
    scenario: Scenario,
    records: list[dict[str, Any]],
    delivered_timestamps: list[int],
) -> dict[str, Any]:
    timestamp_to_frame = {
        timestamp: frame.frame_index
        for timestamp, frame in zip(delivered_timestamps, scenario.frames, strict=False)
    }
    frame_sizes = {frame.frame_index: (frame.width, frame.height) for frame in scenario.frames}
    aliases: dict[str, str] = {}
    objects_by_frame: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    unavailable = False

    for record in records:
        frame_index = timestamp_to_frame.get(record.get("source_timestamp_ns"))
        raw_track_id = record.get("track_id")
        if frame_index is None or not isinstance(raw_track_id, str):
            continue
        if raw_track_id not in aliases:
            if len(aliases) >= MAX_TRACKS_PER_SCENARIO:
                unavailable = True
                break
            aliases[raw_track_id] = f"track-{len(aliases) + 1:03d}"
        clean = _clean_object(record, aliases[raw_track_id], frame_sizes[frame_index])
        if clean is None:
            continue
        objects_by_frame[frame_index][aliases[raw_track_id]] = clean
        if len(objects_by_frame[frame_index]) > MAX_PREDICTIONS_PER_FRAME:
            unavailable = True
            break

    prediction_count = sum(len(objects) for objects in objects_by_frame.values())
    if unavailable or prediction_count > MAX_PREDICTIONS_PER_SCENARIO:
        return _unavailable_overlay(scenario, "budget_exceeded")

    frames = [
        {
            "frame_index": frame.frame_index,
            "source_timestamp_ns": frame.relative_timestamp_ns,
            "objects": list(objects_by_frame.get(frame.frame_index, {}).values()),
        }
        for frame in scenario.frames
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "complete",
        "scenario_id": scenario.id,
        "width": scenario.frames[0].width,
        "height": scenario.frames[0].height,
        "frame_count": len(scenario.frames),
        "frames": frames,
        "summary": {"prediction_count": prediction_count},
    }


def _clean_object(
    record: dict[str, Any],
    track_label: str,
    frame_size: tuple[int, int],
) -> dict[str, Any] | None:
    geometry = record.get("geometry")
    box = geometry.get("value") if isinstance(geometry, dict) else None
    confidence = record.get("confidence")
    event = record.get("event")
    state = record.get("state")
    support = record.get("support")
    if (
        event not in TRACK_OBSERVATION_EVENTS
        or state not in TRACK_STATES
        or support not in SUPPORT_VALUES
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not isinstance(box, list)
        or len(box) != 4
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in box)
        or any(not math.isfinite(float(value)) for value in box)
    ):
        return None
    x1, y1, x2, y2 = (float(value) for value in box)
    width, height = frame_size
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        return None
    raw_class = record.get("class_id")
    class_id = (
        raw_class
        if isinstance(raw_class, str) and SAFE_CLASS.fullmatch(raw_class) and raw_class in PUBLIC_CLASSES
        else "other"
    )
    return {
        "track_label": track_label,
        "class_id": class_id,
        "event": event,
        "state": state,
        "support": support,
        "confidence": round(float(confidence), 4),
        "bbox_xyxy": [round(value, 2) for value in (x1, y1, x2, y2)],
    }


def _unavailable_overlay(scenario: Scenario, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "unavailable",
        "reason": reason,
        "scenario_id": scenario.id,
        "width": scenario.frames[0].width,
        "height": scenario.frames[0].height,
        "frame_count": len(scenario.frames),
        "frames": [],
        "summary": {"prediction_count": 0},
    }


def _encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
