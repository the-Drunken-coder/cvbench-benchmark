from __future__ import annotations

import json
from pathlib import Path

from cvbench.model import CollectedRecord, Frame, Scenario
from cvbench.prediction_overlay import write_prediction_overlays


def test_prediction_overlay_is_frame_locked_and_strictly_sanitized(tmp_path: Path) -> None:
    scenario = Scenario(
        id="rvmot-a1c9",
        family="real_video",
        root=tmp_path,
        frames=[
            Frame("private-sequence", 0, 0, 100, 80, tmp_path / "0.jpg"),
            Frame("private-sequence", 1, 100, 100, 80, tmp_path / "1.jpg"),
        ],
        ground_truth=[],
    )
    secret = "DO-NOT-PUBLISH"
    records = [
        CollectedRecord(
            999_999,
            {
                "schema_version": "cvbench.track/v1",
                "event": "track_started",
                "sequence_id": "private-sequence",
                "source_timestamp_ns": 10_000,
                "track_id": f"</text><script>{secret}</script>",
                "class_id": f"unknown-{secret}",
                "state": "confirmed",
                "support": "observed",
                "confidence": 0.987654,
                "geometry": {"type": "bbox_xyxy", "space": "source_pixels", "value": [1, 2, 40, 50]},
                "arbitrary_extra": secret,
            },
        ),
    ]

    [path] = write_prediction_overlays(
        tmp_path / "overlays",
        [scenario],
        records,
        {"private-sequence": [10_000, 10_100]},
    )
    raw = path.read_text()
    payload = json.loads(raw)

    assert secret not in raw
    assert "private-sequence" not in raw
    assert "999999" not in raw
    assert payload["frames"][0]["objects"] == [{
        "bbox_xyxy": [1.0, 2.0, 40.0, 50.0],
        "class_id": "other",
        "confidence": 0.9877,
        "event": "track_started",
        "state": "confirmed",
        "support": "observed",
        "track_label": "track-001",
    }]
    assert payload["frames"][1]["objects"] == []


def test_prediction_overlay_rejects_out_of_frame_geometry(tmp_path: Path) -> None:
    scenario = Scenario(
        id="rvmot-a1c9",
        family="real_video",
        root=tmp_path,
        frames=[Frame("private-sequence", 0, 0, 100, 80, tmp_path / "0.jpg")],
        ground_truth=[],
    )
    record = CollectedRecord(
        1,
        {
            "event": "track_update",
            "sequence_id": "private-sequence",
            "source_timestamp_ns": 10,
            "track_id": "raw",
            "class_id": "person",
            "state": "confirmed",
            "support": "observed",
            "confidence": 1,
            "geometry": {"value": [-1, 0, 10, 10]},
        },
    )
    [path] = write_prediction_overlays(
        tmp_path / "overlays",
        [scenario],
        [record],
        {"private-sequence": [10]},
    )
    payload = json.loads(path.read_text())
    assert payload["state"] == "complete"
    assert payload["summary"]["prediction_count"] == 0
    assert payload["frames"][0]["objects"] == []
