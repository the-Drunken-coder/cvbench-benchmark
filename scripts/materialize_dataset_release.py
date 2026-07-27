#!/usr/bin/env python3
"""Materialize explicit clips from an installed dataset release for CVBench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import yaml

from cvbench.protocol import validate_ground_truth

try:
    from scripts.install_dataset_release import _verify_release, load_lock
except ModuleNotFoundError:  # Direct `python scripts/materialize_dataset_release.py` execution.
    from install_dataset_release import _verify_release, load_lock

Decoder = Callable[[Path, dict[str, int]], list[bytes]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_video(path: Path, media: dict[str, int]) -> list[bytes]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"could not decode dataset video: {path}")
    frames: list[bytes] = []
    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break
            if image.shape[:2] != (media["height"], media["width"]):
                raise ValueError(f"decoded frame dimensions do not match source.json: {path}")
            encoded, body = cv2.imencode(
                ".jpg",
                image,
                [cv2.IMWRITE_JPEG_QUALITY, 95],
            )
            if not encoded:
                raise ValueError(f"could not encode dataset frame: {path}")
            frames.append(body.tobytes())
    finally:
        capture.release()
    if len(frames) != media["frame_count"]:
        raise ValueError(
            f"decoded frame count does not match source.json: expected "
            f"{media['frame_count']}, found {len(frames)}"
        )
    return frames


def _load_annotations(path: Path, clip_id: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    if not rows:
        raise ValueError(f"dataset clip has no truth rows: {clip_id}")
    keys: set[tuple[int, str]] = set()
    for row in rows:
        required = {
            "bbox_xyxy",
            "class_id",
            "clip_id",
            "frame_index",
            "label_origin",
            "schema_version",
            "source_timestamp_ns",
            "track_id",
        }
        if not isinstance(row, dict) or not required <= set(row):
            raise ValueError(f"dataset truth row is incomplete: {clip_id}")
        if row["schema_version"] != "cvbench.track-annotation/v1" or row["clip_id"] != clip_id:
            raise ValueError(f"dataset truth row has the wrong identity: {clip_id}")
        key = (row["frame_index"], row["track_id"])
        if key in keys:
            raise ValueError(f"dataset truth has a duplicate frame/track row: {clip_id}")
        keys.add(key)
    return sorted(rows, key=lambda row: (row["frame_index"], row["track_id"]))


def convert_annotations(
    rows: list[dict[str, Any]],
    *,
    scenario_id: str,
    frame_timestamps: list[int],
) -> list[dict[str, Any]]:
    by_track: defaultdict[str, list[int]] = defaultdict(list)
    for row in rows:
        frame_index = row["frame_index"]
        if (
            not isinstance(frame_index, int)
            or isinstance(frame_index, bool)
            or not 0 <= frame_index < len(frame_timestamps)
            or row["source_timestamp_ns"] != frame_timestamps[frame_index]
        ):
            raise ValueError("dataset truth timestamp does not match certified media cadence")
        by_track[row["track_id"]].append(frame_index)
    bounds = {
        track_id: (min(indices), max(indices))
        for track_id, indices in by_track.items()
    }
    converted = []
    for row in rows:
        frame_index = row["frame_index"]
        first, last = bounds[row["track_id"]]
        output = {
            "schema_version": "cvbench.ground-truth/v1",
            "target_id": row["track_id"],
            "sequence_id": scenario_id,
            "source_timestamp_ns": row["source_timestamp_ns"],
            "on_screen": True,
            "eligible_for_detection": True,
            "visibility_fraction": None,
            "occlusion": "unknown",
            "class_id": row["class_id"],
            "bbox_xyxy": row["bbox_xyxy"],
            "entry_event": frame_index == first,
            "exit_event": frame_index == last,
            "truncated": bool(row.get("truncated", False)),
        }
        validate_ground_truth(output)
        converted.append(output)
    return converted


def _validated_source(path: Path, clip_id: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("schema_version") != "cvbench.source/v1" or value.get("clip_id") != clip_id:
        raise ValueError(f"clip source.json has the wrong identity: {clip_id}")
    media = value.get("media")
    required = {"fps_denominator", "fps_numerator", "frame_count", "height", "width"}
    if (
        not isinstance(media, dict)
        or set(media) != required
        or not all(
            isinstance(media[field], int)
            and not isinstance(media[field], bool)
            and media[field] > 0
            for field in required
        )
    ):
        raise ValueError(f"clip source.json has invalid media metadata: {clip_id}")
    return value


def _write_clip(
    dataset: dict[str, Any],
    dataset_root: Path,
    clip_id: str,
    output: Path,
    decoder: Decoder,
) -> dict[str, Any]:
    clip_root = dataset_root / "clips" / clip_id
    source = _validated_source(clip_root / "source.json", clip_id)
    media = source["media"]
    frames = decoder(clip_root / "video.mp4", media)
    if len(frames) != media["frame_count"]:
        raise ValueError(f"decoder returned the wrong frame count: {clip_id}")
    timestamps = [
        round(index * 1_000_000_000 * media["fps_denominator"] / media["fps_numerator"])
        for index in range(media["frame_count"])
    ]
    scenario_id = f"{dataset['id']}--{clip_id}"
    rows = convert_annotations(
        _load_annotations(clip_root / "tracks.jsonl", clip_id),
        scenario_id=scenario_id,
        frame_timestamps=timestamps,
    )
    clip_output = output / clip_id
    frames_output = clip_output / "frames"
    frames_output.mkdir(parents=True)
    frame_entries = []
    for index, (timestamp, body) in enumerate(zip(timestamps, frames, strict=True)):
        name = f"frame-{index:06d}.jpg"
        (frames_output / name).write_bytes(body)
        frame_entries.append(
            {
                "frame_index": index,
                "source_timestamp_ns": timestamp,
                "width": media["width"],
                "height": media["height"],
                "path": f"frames/{name}",
            }
        )
    (clip_output / "ground_truth.jsonl").write_text(
        "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows)
    )
    scenario = {
        "schema_version": "cvbench.scenario/v1",
        "id": scenario_id,
        "family": dataset["id"],
        "sequence_id": scenario_id,
        "license": source["source"]["license"]["spdx"],
        "source": (
            f"{source['source']['title']}; certified dataset "
            f"{dataset['id']} {dataset['version']}"
        ),
        "annotation_scope": "exhaustive_visible",
        "ontology": [item["id"] for item in dataset["ontology"]["classes"]],
        "ground_truth": "ground_truth.jsonl",
        "frames": frame_entries,
    }
    (clip_output / "scenario.yaml").write_text(yaml.safe_dump(scenario, sort_keys=False))
    return {
        "clip_id": clip_id,
        "scenario_id": scenario_id,
        "frames": len(frames),
        "truth_rows": len(rows),
    }


def materialize(
    repo_root: Path,
    lock_path: Path,
    clip_ids: list[str],
    *,
    replace: bool = False,
    decoder: Decoder = _decode_video,
) -> Path:
    if not clip_ids or len(set(clip_ids)) != len(clip_ids):
        raise ValueError("materialization requires a non-empty unique explicit clip list")
    repo_root = repo_root.resolve()
    lock = load_lock(lock_path.resolve())
    dataset_root = (repo_root / lock["install_path"]).resolve()
    if not dataset_root.is_dir():
        raise FileNotFoundError(f"install the locked dataset release first: {dataset_root}")
    installed_lock_path = dataset_root / ".cvbench-dataset-lock.json"
    if not installed_lock_path.is_file() or json.loads(installed_lock_path.read_text()) != lock:
        raise ValueError("installed dataset is not bound to the requested lock")
    _verify_release(dataset_root, lock)
    dataset = yaml.safe_load((dataset_root / "dataset.yaml").read_text())
    if (
        not isinstance(dataset, dict)
        or dataset.get("schema_version") != "cvbench.dataset/v1"
        or dataset.get("id") != lock["dataset_id"]
        or dataset.get("version") != lock["version"]
        or dataset.get("state") != "certified"
        or dataset.get("data_role") != "benchmark_truth"
        or dataset.get("annotation_scope") != "exhaustive_visible"
        or dataset.get("evaluation_eligible") is not True
    ):
        raise ValueError(
            "installed dataset must be certified, evaluation-eligible, "
            "exhaustive-visible benchmark truth"
        )
    declared_clips = {
        item["id"]: item["path"]
        for item in dataset.get("clips", [])
        if isinstance(item, dict) and set(item) == {"id", "path"}
    }
    if any(declared_clips.get(clip_id) != f"clips/{clip_id}" for clip_id in clip_ids):
        raise ValueError("requested clip is not explicitly declared by the dataset")
    destination = (
        repo_root
        / "data"
        / "materialized"
        / lock["dataset_id"]
        / lock["version"]
    ).resolve()
    expected_parent = (repo_root / "data" / "materialized" / lock["dataset_id"]).resolve()
    if destination.parent != expected_parent:
        raise ValueError("materialized dataset destination escaped its dedicated root")
    with tempfile.TemporaryDirectory(prefix="cvbench-materialize-") as temporary_name:
        temporary = Path(temporary_name) / "output"
        temporary.mkdir()
        summaries = [
            _write_clip(dataset, dataset_root, clip_id, temporary, decoder)
            for clip_id in clip_ids
        ]
        files = [
            {
                "path": path.relative_to(temporary).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in sorted(temporary.rglob("*"))
            if path.is_file()
        ]
        (temporary / "materialization.json").write_text(
            json.dumps(
                {
                    "schema_version": "cvbench.dataset-materialization/v1",
                    "dataset": {
                        "id": lock["dataset_id"],
                        "version": lock["version"],
                    },
                    "release_manifest_sha256": _sha256(dataset_root / "release-manifest.json"),
                    "lock_sha256": _sha256(lock_path),
                    "clips": summaries,
                    "files": files,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not replace:
                raise FileExistsError(f"dataset is already materialized: {destination}")
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--clip", action="append", required=True, dest="clips")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(materialize(args.repo_root, args.lock, args.clips, replace=args.replace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
