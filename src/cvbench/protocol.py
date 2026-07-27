from __future__ import annotations

import json
import math
import socket
import struct
from typing import Any, BinaryIO

from .errors import ProtocolError

TRACK_EVENTS = {"track_started", "track_update", "track_reacquired", "track_ended"}
TRACK_OBSERVATION_EVENTS = {"track_started", "track_update", "track_reacquired"}
EVENTS = TRACK_EVENTS | {"system_status", "system_error"}
TRACK_STATES = {"tentative", "confirmed", "coasting", "reacquired", "lost"}
SUPPORT_VALUES = {"observed", "predicted"}
OCCLUSION_VALUES = {"none", "partial", "full", "unknown"}
MAX_HEADER_BYTES = 1_000_000
MAX_PAYLOAD_BYTES = 100_000_000


def _require(record: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in record:
        raise ProtocolError(f"missing required field: {key}")
    value = record[key]
    numeric = kind in (int, float) or kind == (int, float)
    if not isinstance(value, kind) or (isinstance(value, bool) and numeric):
        raise ProtocolError(f"{key} has invalid type")
    return value


def validate_bbox(
    value: Any, *, width: int | None = None, height: int | None = None, out_of_bounds: str = "reject"
) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ProtocolError("bounding box must contain four coordinates")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in value):
        raise ProtocolError("bounding-box coordinates must be numbers")
    box = [float(v) for v in value]
    if not all(math.isfinite(v) for v in box):
        raise ProtocolError("bounding-box coordinates must be finite")
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ProtocolError("bounding-box coordinates are not ordered")
    if width is not None and height is not None:
        outside = box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height
        if outside and out_of_bounds == "reject":
            raise ProtocolError("bounding box is outside the source frame")
        if outside and out_of_bounds == "clip":
            box = [max(0.0, box[0]), max(0.0, box[1]), min(width, box[2]), min(height, box[3])]
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ProtocolError("clipped bounding box is empty")
    return box


def validate_track_record(
    record: Any, *, frame_size: tuple[int, int] | None = None, out_of_bounds: str = "reject"
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolError("output record must be a JSON object")
    if record.get("schema_version") != "cvbench.track/v1":
        raise ProtocolError("schema_version must be cvbench.track/v1")
    event = _require(record, "event", str)
    if event not in EVENTS:
        raise ProtocolError(f"unsupported event: {event}")
    if not _require(record, "sequence_id", str):
        raise ProtocolError("sequence_id must be a non-empty string")
    timestamp = _require(record, "source_timestamp_ns", int)
    if timestamp < 0:
        raise ProtocolError("source_timestamp_ns must be non-negative")
    if event in TRACK_EVENTS:
        if not _require(record, "track_id", str):
            raise ProtocolError("track_id must be a non-empty string")
        if _require(record, "state", str) not in TRACK_STATES:
            raise ProtocolError("invalid track state")
        if _require(record, "support", str) not in SUPPORT_VALUES:
            raise ProtocolError("invalid track support")
        _require(record, "class_id", str)
        confidence = _require(record, "confidence", (int, float))
        if not math.isfinite(float(confidence)) or not 0 <= confidence <= 1:
            raise ProtocolError("confidence must be finite and between zero and one")
        geometry = _require(record, "geometry", dict)
        if geometry.get("type") != "bbox_xyxy" or geometry.get("space") != "source_pixels":
            raise ProtocolError("only source-pixel bbox_xyxy geometry is supported")
        size = frame_size or (None, None)
        clean = dict(record)
        clean["geometry"] = dict(geometry)
        clean["geometry"]["value"] = validate_bbox(
            geometry.get("value"), width=size[0], height=size[1], out_of_bounds=out_of_bounds
        )
        return clean
    return dict(record)


def validate_ground_truth(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ProtocolError("ground-truth record must be a JSON object")
    required = {
        "target_id": str,
        "sequence_id": str,
        "source_timestamp_ns": int,
        "on_screen": bool,
        "eligible_for_detection": bool,
        "occlusion": str,
        "class_id": str,
    }
    for key, kind in required.items():
        _require(record, key, kind)
    ignore = record.get("ignore", False)
    if not isinstance(ignore, bool):
        raise ProtocolError("ignore must be boolean")
    ignore_region = record.get("ignore_region", False)
    if not isinstance(ignore_region, bool):
        raise ProtocolError("ignore_region must be boolean")
    ignore_region_id = record.get("ignore_region_id")
    truncated = record.get("truncated", False)
    if not isinstance(truncated, bool):
        raise ProtocolError("truncated must be boolean")
    if ignore_region and not ignore:
        raise ProtocolError("ignore_region requires ignore=true")
    if ignore_region and (not isinstance(ignore_region_id, str) or not ignore_region_id):
        raise ProtocolError("ignore_region requires a non-empty ignore_region_id")
    if ignore_region_id is not None and not isinstance(ignore_region_id, str):
        raise ProtocolError("ignore_region_id must be a string")
    if ignore_region_id is not None and not ignore_region:
        raise ProtocolError("ignore_region_id requires ignore_region=true")
    if "visibility_fraction" not in record:
        raise ProtocolError("missing visibility_fraction")
    visibility = record["visibility_fraction"]
    if visibility is None:
        if record["occlusion"] != "unknown":
            raise ProtocolError("unknown visibility requires unknown occlusion")
    elif not isinstance(visibility, (int, float)) or isinstance(visibility, bool):
        raise ProtocolError("visibility_fraction has invalid type")
    elif not math.isfinite(float(visibility)) or not 0 <= float(visibility) <= 1:
        raise ProtocolError("visibility_fraction must be between zero and one")
    if record["occlusion"] not in OCCLUSION_VALUES:
        raise ProtocolError("invalid occlusion state")
    clean = dict(record)
    clean["ignore"] = ignore
    clean["ignore_region"] = ignore_region
    clean["truncated"] = truncated
    clean["visibility_fraction"] = visibility
    if record["on_screen"]:
        clean["bbox_xyxy"] = validate_bbox(record.get("bbox_xyxy"))
    elif record.get("bbox_xyxy") is not None:
        clean["bbox_xyxy"] = validate_bbox(record["bbox_xyxy"])
    return clean


def encode_message(metadata: dict[str, Any], payload: bytes = b"") -> bytes:
    header = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode()
    if len(header) > MAX_HEADER_BYTES or len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("frame message exceeds protocol limit")
    return struct.pack("!II", len(header), len(payload)) + header + payload


def send_message(sock: socket.socket, metadata: dict[str, Any], payload: bytes = b"") -> None:
    sock.sendall(encode_message(metadata, payload))


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk:
            raise EOFError("frame stream closed")
        data.extend(chunk)
    return bytes(data)


def receive_message(stream: BinaryIO) -> tuple[dict[str, Any], bytes]:
    prefix = _read_exact(stream, 8)
    header_length, payload_length = struct.unpack("!II", prefix)
    if header_length > MAX_HEADER_BYTES or payload_length > MAX_PAYLOAD_BYTES:
        raise ProtocolError("frame message exceeds protocol limit")
    try:
        metadata = json.loads(_read_exact(stream, header_length))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("invalid frame metadata JSON") from exc
    if not isinstance(metadata, dict):
        raise ProtocolError("frame metadata must be an object")
    return metadata, _read_exact(stream, payload_length)
