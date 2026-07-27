from __future__ import annotations

from typing import Any

from cvbench.model import CollectedRecord


def gt(
    timestamp_ns: int,
    *,
    target: str = "gt-1",
    sequence: str = "seq",
    box: list[float] | None = None,
    eligible: bool = True,
    visibility: float = 1.0,
    occlusion: str = "none",
) -> dict[str, Any]:
    return {
        "target_id": target,
        "sequence_id": sequence,
        "source_timestamp_ns": timestamp_ns,
        "on_screen": True,
        "eligible_for_detection": eligible,
        "visibility_fraction": visibility,
        "occlusion": occlusion,
        "class_id": "person",
        "bbox_xyxy": box or [10.0, 10.0, 20.0, 20.0],
    }


def output(
    timestamp_ns: int,
    *,
    track: str = "track-1",
    sequence: str = "seq",
    box: list[float] | None = None,
    state: str = "confirmed",
    support: str = "observed",
    received_offset_ns: int = 5_000_000,
) -> CollectedRecord:
    record = {
        "schema_version": "cvbench.track/v1",
        "event": "track_update",
        "sequence_id": sequence,
        "source_timestamp_ns": timestamp_ns,
        "track_id": track,
        "state": state,
        "support": support,
        "class_id": "person",
        "confidence": 0.9,
        "geometry": {"type": "bbox_xyxy", "space": "source_pixels", "value": box or [10.0, 10.0, 20.0, 20.0]},
    }
    return CollectedRecord(timestamp_ns + received_offset_ns, record)
