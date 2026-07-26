from __future__ import annotations

import json
from pathlib import Path

import cvbench.prediction_overlay as prediction_overlay
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


def test_prediction_overlay_clamps_confidence_and_rejects_rounding_collapsed_boxes(tmp_path: Path) -> None:
    scenario = Scenario(
        id="rvmot-a1c9",
        family="real_video",
        root=tmp_path,
        frames=[Frame("private-sequence", 0, 0, 100, 80, tmp_path / "0.jpg")],
        ground_truth=[],
    )

    def record(track_id: str, confidence: float, box: list[float]) -> CollectedRecord:
        return CollectedRecord(1, {
            "event": "track_update",
            "sequence_id": "private-sequence",
            "source_timestamp_ns": 10,
            "track_id": track_id,
            "class_id": "person",
            "state": "confirmed",
            "support": "observed",
            "confidence": confidence,
            "geometry": {"value": box},
        })

    [path] = write_prediction_overlays(
        tmp_path / "overlays",
        [scenario],
        [
            record("kept", 1.7, [1, 2, 10, 20]),
            record("collapsed", -0.4, [5.001, 6, 5.004, 7]),
        ],
        {"private-sequence": [10]},
    )
    objects = json.loads(path.read_text())["frames"][0]["objects"]
    assert objects == [{
        "bbox_xyxy": [1.0, 2.0, 10.0, 20.0],
        "class_id": "person",
        "confidence": 1.0,
        "event": "track_update",
        "state": "confirmed",
        "support": "observed",
        "track_label": "track-001",
    }]


def test_prediction_overlay_explicitly_downgrades_budget_overflow(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(
        id="rvmot-a1c9",
        family="real_video",
        root=tmp_path,
        frames=[Frame("private-sequence", 0, 0, 100, 80, tmp_path / "0.jpg")],
        ground_truth=[],
    )
    records = [
        CollectedRecord(1, {
            "event": "track_update",
            "sequence_id": "private-sequence",
            "source_timestamp_ns": 10,
            "track_id": track_id,
            "class_id": "person",
            "state": "confirmed",
            "support": "observed",
            "confidence": 0.9,
            "geometry": {"value": [1, 2, 10, 20]},
        })
        for track_id in ("one", "two")
    ]

    monkeypatch.setattr(prediction_overlay, "MAX_PREDICTIONS_PER_FRAME", 1)
    [path] = write_prediction_overlays(
        tmp_path / "count-overflow",
        [scenario],
        records,
        {"private-sequence": [10]},
    )
    assert json.loads(path.read_text())["reason"] == "budget_exceeded"

    monkeypatch.setattr(prediction_overlay, "MAX_PREDICTIONS_PER_FRAME", 128)
    monkeypatch.setattr(prediction_overlay, "MAX_SCENARIO_BYTES", 1)
    [path] = write_prediction_overlays(
        tmp_path / "byte-overflow",
        [scenario],
        records[:1],
        {"private-sequence": [10]},
    )
    assert json.loads(path.read_text())["reason"] == "budget_exceeded"

    monkeypatch.setattr(prediction_overlay, "MAX_SCENARIO_BYTES", 1536 * 1024)
    monkeypatch.setattr(prediction_overlay, "MAX_TOTAL_BYTES", 1)
    paths = write_prediction_overlays(
        tmp_path / "total-overflow",
        [
            scenario,
            Scenario(
                id="rvmot-b7e2",
                family="real_video",
                root=tmp_path,
                frames=[Frame("private-sequence", 0, 0, 100, 80, tmp_path / "0.jpg")],
                ground_truth=[],
            ),
        ],
        records[:1],
        {"private-sequence": [10]},
    )
    assert all(json.loads(path.read_text())["reason"] == "budget_exceeded" for path in paths)
