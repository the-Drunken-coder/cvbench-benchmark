from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from cvbench.scenario import load_scenario
from scripts.prepare_bdd100k_mot import ImportFailure, prepare


def _label(
    identifier: str,
    category: str,
    box: tuple[int, int, int, int],
    *,
    occluded: bool = False,
    truncated: bool = False,
    crowd: bool = False,
) -> dict[str, object]:
    x1, y1, x2, y2 = box
    return {
        "id": identifier,
        "category": category,
        "attributes": {
            "Occluded": occluded,
            "Truncated": truncated,
            "Crowd": crowd,
        },
        "box2d": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
    }


def _source(
    root: Path,
    *,
    split: str = "train",
    category: str = "pedestrian",
    timestamps: tuple[int, int] | None = None,
) -> Path:
    source = root / "bdd100k"
    images = source / "images" / "track" / split / "clip-a"
    labels = source / "labels" / "box_track_20" / split
    images.mkdir(parents=True)
    labels.mkdir(parents=True)
    names = ("clip-a-0000000.jpg", "clip-a-0000002.jpg")
    for offset, name in enumerate(names):
        image = np.full((24, 32, 3), 40 + offset * 20, dtype=np.uint8)
        assert cv2.imwrite(str(images / name), image)
    frames: list[dict[str, object]] = [
        {
            "name": names[0],
            "videoName": "clip-a",
            "frameIndex": 0,
            "labels": [
                _label("person-1", category, (1, 2, 10, 20), occluded=True),
                _label("distractor-1", "other vehicle", (12, 3, 25, 19), truncated=True),
            ],
        },
        {
            "name": names[1],
            "videoName": "clip-a",
            "frameIndex": 2,
            "labels": [
                _label("person-1", category, (2, 2, 11, 20)),
                _label("car-1", "car", (14, 4, 27, 21)),
            ],
        },
    ]
    if timestamps is not None:
        for frame, timestamp in zip(frames, timestamps, strict=True):
            frame["timestamp"] = timestamp
    (labels / "clip-a.json").write_text(json.dumps(frames))
    return source


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_prepare_is_deterministic_and_does_not_interpolate_sparse_frames(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source")
    first = tmp_path / "prepared-a"
    second = tmp_path / "prepared-b"

    first_manifest = prepare(source, first, ("train",))
    second_manifest = prepare(source, second, ("train",))

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    first_files = {
        path.relative_to(first): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files

    scenario_path = first / "train" / "clip-a" / "scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    assert [frame["frame_index"] for frame in scenario["frames"]] == [0, 2]
    assert [frame["source_timestamp_ns"] for frame in scenario["frames"]] == [
        0,
        400_000_000,
    ]
    assert scenario["timestamp_policy"] == "bdd100k_mot_2020_frame_index_at_5hz"
    assert len(load_scenario(scenario_path).frames) == 2


def test_prepare_preserves_tracks_attributes_and_explicit_exclusions(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source")
    output = tmp_path / "prepared"
    prepare(source, output, ("train",))

    rows = _jsonl(output / "train" / "clip-a" / "ground_truth.jsonl")
    first_person = next(
        row
        for row in rows
        if row["source_frame_index"] == 0 and row["source_track_id"] == "person-1"
    )
    assert first_person["target_id"] == "bdd100k:clip-a:person-1"
    assert first_person["class_id"] == "person"
    assert first_person["bbox_xyxy"] == [1.0, 2.0, 10.0, 20.0]
    assert first_person["occlusion"] == "partial"
    assert first_person["visibility_fraction"] is None
    assert first_person["truncated"] is False
    assert first_person["source_attributes"] == {
        "crowd": False,
        "occluded": True,
        "truncated": False,
    }
    distractor = next(row for row in rows if row["source_track_id"] == "distractor-1")
    assert distractor["class_id"] == "bdd100k-excluded/other-vehicle"
    assert distractor["ignore"] is True
    assert distractor["eligible_for_detection"] is False
    assert distractor["truncated"] is True
    assert len(rows) == 4

    manifest = json.loads((output / "corpus-manifest.json").read_text())
    assert manifest["use"] == "training_only"
    assert manifest["benchmark_eligible"] is False
    assert manifest["totals"] == {"frames": 2, "labels": 4, "sequences": 1}
    assert len(manifest["source_inventory"]) == 3
    assert all(item["sha256"] for item in manifest["source_inventory"])
    assert manifest["category_policy"]["other vehicle"]["ignore"] is True
    assert "commercial" in manifest["license_notice"]["boundary"].lower()


def test_prepare_uses_exact_scalabel_timestamp_deltas(tmp_path: Path) -> None:
    source = _source(
        tmp_path / "source",
        timestamps=(1_700_000_000_000, 1_700_000_000_237),
    )
    output = tmp_path / "prepared"
    prepare(source, output, ("train",))

    scenario = yaml.safe_load(
        (output / "train" / "clip-a" / "scenario.yaml").read_text()
    )
    assert [frame["source_timestamp_ns"] for frame in scenario["frames"]] == [
        0,
        237_000_000,
    ]
    assert [frame["native_source_timestamp_ms"] for frame in scenario["frames"]] == [
        1_700_000_000_000,
        1_700_000_000_237,
    ]
    assert scenario["timestamp_policy"] == "scalabel.timestamp_ms"


def test_prepare_rejects_unknown_categories_without_partial_output(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "source", category="traffic light")
    output = tmp_path / "prepared"

    with pytest.raises(ImportFailure, match="unsupported category"):
        prepare(source, output, ("train",))

    assert not output.exists()


def test_prepare_rejects_missing_images_and_existing_output(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    missing = (
        source
        / "images"
        / "track"
        / "train"
        / "clip-a"
        / "clip-a-0000002.jpg"
    )
    missing.unlink()
    output = tmp_path / "prepared"
    with pytest.raises(ImportFailure, match="inventory differs"):
        prepare(source, output, ("train",))
    assert not output.exists()

    replacement = _source(tmp_path / "replacement")
    output.mkdir()
    with pytest.raises(ImportFailure, match="refusing to overwrite"):
        prepare(replacement, output, ("train",))
