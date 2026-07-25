from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError, ProtocolError
from .model import Frame, Scenario
from .protocol import validate_ground_truth


def _object_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConfigurationError(f"scenario {key} must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ConfigurationError(f"scenario {key} entries must be objects")
    return value


def _scoreable_roi(data: dict[str, Any], width: int, height: int) -> tuple[float, float, float, float] | None:
    value = data.get("scoreable_roi")
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ConfigurationError("scenario scoreable_roi must be [x1, y1, x2, y2]")
    try:
        roi = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("scenario scoreable_roi must contain numbers") from exc
    if not (0 <= roi[0] < roi[2] <= width and 0 <= roi[1] < roi[3] <= height):
        raise ConfigurationError("scenario scoreable_roi must be inside the frame")
    return roi


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load scenario {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != "cvbench.scenario/v1":
        raise ConfigurationError(f"{path} is not a cvbench.scenario/v1 manifest")
    root = path.parent
    raw_frames = _object_list(data, "frames")
    faults = _object_list(data, "faults")
    frames: list[Frame] = []
    last_timestamp = -1
    for raw in raw_frames:
        try:
            frame = Frame(
                sequence_id=data["sequence_id"],
                frame_index=int(raw["frame_index"]),
                relative_timestamp_ns=int(raw["source_timestamp_ns"]),
                width=int(raw["width"]),
                height=int(raw["height"]),
                path=(root / raw["path"]).resolve(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid frame in {path}: {exc}") from exc
        if frame.relative_timestamp_ns <= last_timestamp:
            raise ConfigurationError(f"frame timestamps in {path} must be strictly increasing")
        if not frame.path.is_file():
            raise ConfigurationError(f"scenario frame does not exist: {frame.path}")
        last_timestamp = frame.relative_timestamp_ns
        frames.append(frame)
    if not frames:
        raise ConfigurationError(f"scenario {path} has no frames")
    scoreable_roi = _scoreable_roi(data, frames[0].width, frames[0].height)
    frame_keys = {(frame.sequence_id, frame.relative_timestamp_ns) for frame in frames}
    gt_path = root / data.get("ground_truth", "ground_truth.jsonl")
    ground_truth: list[dict[str, Any]] = []
    try:
        for _line_number, line in enumerate(gt_path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            record = validate_ground_truth(json.loads(line))
            if record["sequence_id"] != data["sequence_id"]:
                raise ProtocolError("ground truth sequence_id does not match scenario")
            if (record["sequence_id"], record["source_timestamp_ns"]) not in frame_keys:
                raise ProtocolError("ground truth timestamp does not match a scenario frame")
            ground_truth.append(record)
    except (OSError, json.JSONDecodeError, ProtocolError) as exc:
        raise ConfigurationError(f"invalid ground truth {gt_path}: {exc}") from exc
    return Scenario(
        id=str(data["id"]),
        family=str(data["family"]),
        root=root,
        frames=frames,
        ground_truth=ground_truth,
        ground_truth_path=gt_path.resolve(),
        faults=faults,
        scoreable_roi=scoreable_roi,
    )
