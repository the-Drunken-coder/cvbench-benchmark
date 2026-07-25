#!/usr/bin/env python3
"""Prepare user-supplied BDD100K MOT 2020 train/val data for CVBench training."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import yaml

IMPORTER_VERSION = "cvbench.bdd100k-mot-import/v1"
MAPPING_POLICY_VERSION = "cvbench.bdd100k-mot-person-vehicle/v1"
ANNOTATED_FPS = 5
SPLITS = ("train", "val")

# BDD100K MOT 2020 evaluation calls the final three categories distractors. They
# remain explicit ignored rows instead of disappearing into background.
CATEGORY_POLICY: dict[str, tuple[str, bool]] = {
    "pedestrian": ("person", False),
    "rider": ("person", False),
    "car": ("vehicle", False),
    "bus": ("vehicle", False),
    "truck": ("vehicle", False),
    "train": ("vehicle", False),
    "motorcycle": ("vehicle", False),
    "bicycle": ("vehicle", False),
    "other person": ("bdd100k-excluded/other-person", True),
    "trailer": ("bdd100k-excluded/trailer", True),
    "other vehicle": ("bdd100k-excluded/other-vehicle", True),
}

LICENSE_NOTICE = {
    "source": "https://bdd-data.berkeley.edu/download.html",
    "copyright": "Copyright 2018. The Regents of the University of California. All Rights Reserved.",
    "boundary": (
        "Educational, research, and not-for-profit use is granted without a signed agreement. "
        "Commercial use is granted only to BDD and BAIR Commons members and affiliates; "
        "others must contact UC Berkeley for commercial licensing."
    ),
    "commercial_contact": "otl@berkeley.edu",
    "notice": "The user is responsible for confirming that the intended training use is licensed.",
}


class ImportFailure(RuntimeError):
    """A source corpus failed a closed validation check."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _plain_file(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ImportFailure(f"{description} must be a regular, non-symlink file: {path}")


def _plain_directory(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ImportFailure(f"{description} must be a regular, non-symlink directory: {path}")


def _strict_children(path: Path, *, suffix: str | None = None, directories: bool = False) -> list[Path]:
    children = sorted(path.iterdir(), key=lambda item: item.name)
    for child in children:
        if child.is_symlink():
            raise ImportFailure(f"source symlinks are not accepted: {child}")
        if directories and not child.is_dir():
            raise ImportFailure(f"unexpected file in directory-only source tree: {child}")
        if not directories and not child.is_file():
            raise ImportFailure(f"unexpected directory in file-only source tree: {child}")
        if suffix is not None and child.suffix.lower() != suffix:
            raise ImportFailure(f"unexpected source file extension: {child}")
    return children


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ImportFailure(f"{field} must be an integer")
    return value


def _frame_index(frame: dict[str, Any], context: str) -> tuple[int, str]:
    keys = [key for key in ("frameIndex", "index") if key in frame]
    if len(keys) != 1:
        raise ImportFailure(f"{context} must contain exactly one of frameIndex or index")
    value = _integer(frame[keys[0]], f"{context}.{keys[0]}")
    if value < 0:
        raise ImportFailure(f"{context}.{keys[0]} must be non-negative")
    return value, keys[0]


def _boolean_attribute(
    attributes: dict[str, Any], lower: str, upper: str, context: str, *, required: bool
) -> bool:
    present = [key for key in (lower, upper) if key in attributes]
    if len(present) > 1:
        raise ImportFailure(f"{context} contains ambiguous {lower}/{upper} attributes")
    if not present:
        if required:
            raise ImportFailure(f"{context} is missing {lower}")
        return False
    value = attributes[present[0]]
    if not isinstance(value, bool):
        raise ImportFailure(f"{context}.{present[0]} must be boolean")
    return value


def _box(label: dict[str, Any], width: int, height: int, context: str) -> list[float]:
    box = label.get("box2d")
    if not isinstance(box, dict):
        raise ImportFailure(f"{context}.box2d must be an object")
    if set(box) != {"x1", "y1", "x2", "y2"}:
        raise ImportFailure(f"{context}.box2d must contain exactly x1, y1, x2, y2")
    values: list[float] = []
    for key in ("x1", "y1", "x2", "y2"):
        value = box[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ImportFailure(f"{context}.box2d.{key} must be a finite number")
        values.append(float(value))
    x1, y1, x2, y2 = values
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ImportFailure(f"{context}.box2d is outside the {width}x{height} source frame")
    return values


def _track_id(value: Any, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
        raise ImportFailure(f"{context}.id must be a non-empty string or integer")
    return str(value)


def _load_frames(label_path: Path) -> list[dict[str, Any]]:
    try:
        frames = json.loads(label_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportFailure(f"cannot parse Scalabel JSON {label_path}: {exc}") from exc
    if not isinstance(frames, list) or not frames:
        raise ImportFailure(f"Scalabel label file must be a non-empty frame list: {label_path}")
    if not all(isinstance(frame, dict) for frame in frames):
        raise ImportFailure(f"Scalabel frame entries must be objects: {label_path}")
    return frames


def _image_dimensions(path: Path) -> tuple[int, int]:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim not in (2, 3):
        raise ImportFailure(f"cannot decode JPEG image: {path}")
    height, width = image.shape[:2]
    if width <= 0 or height <= 0:
        raise ImportFailure(f"invalid JPEG dimensions: {path}")
    return width, height


def _timestamps(frames: list[dict[str, Any]], indices: list[int], context: str) -> tuple[list[int], str]:
    present = ["timestamp" in frame for frame in frames]
    if any(present) and not all(present):
        raise ImportFailure(f"{context} mixes frames with and without Scalabel timestamps")
    if all(present):
        native_ms = [_integer(frame["timestamp"], f"{context}.timestamp") for frame in frames]
        if any(value < 0 for value in native_ms):
            raise ImportFailure(f"{context} timestamps must be non-negative")
        if any(right <= left for left, right in zip(native_ms, native_ms[1:], strict=False)):
            raise ImportFailure(f"{context} timestamps must be strictly increasing")
        origin = native_ms[0]
        return [(value - origin) * 1_000_000 for value in native_ms], "scalabel.timestamp_ms"
    origin = indices[0]
    return [
        round((index - origin) * 1_000_000_000 / ANNOTATED_FPS) for index in indices
    ], "bdd100k_mot_2020_frame_index_at_5hz"


def _sequence(
    *,
    source_root: Path,
    stage: Path,
    split: str,
    label_path: Path,
    image_directory: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    video = label_path.stem
    frames = _load_frames(label_path)
    parsed: list[tuple[int, dict[str, Any], str]] = []
    frame_index_key: str | None = None
    for offset, frame in enumerate(frames):
        context = f"{label_path}:{offset}"
        index, key = _frame_index(frame, context)
        if frame_index_key is not None and key != frame_index_key:
            raise ImportFailure(f"{label_path} mixes frameIndex and index")
        frame_index_key = key
        name = frame.get("name")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ImportFailure(f"{context}.name must be a plain image filename")
        video_name = frame.get("videoName")
        if not isinstance(video_name, str) or video_name != video:
            raise ImportFailure(f"{context}.videoName must equal {video!r}")
        labels = frame.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, dict) for label in labels):
            raise ImportFailure(f"{context}.labels must be a list of objects")
        parsed.append((index, frame, name))
    parsed.sort(key=lambda item: item[0])
    indices = [item[0] for item in parsed]
    names = [item[2] for item in parsed]
    if len(indices) != len(set(indices)) or len(names) != len(set(names)):
        raise ImportFailure(f"{label_path} contains duplicate frame indices or names")

    image_files = _strict_children(image_directory, suffix=".jpg")
    actual_names = [path.name for path in image_files]
    if actual_names != sorted(names):
        missing = sorted(set(names) - set(actual_names))
        extra = sorted(set(actual_names) - set(names))
        raise ImportFailure(
            f"{video} image/label inventory differs; missing={missing[:3]}, extra={extra[:3]}"
        )

    ordered_frames = [item[1] for item in parsed]
    relative_ns, timestamp_policy = _timestamps(ordered_frames, indices, str(label_path))
    sequence_id = f"bdd100k-mot2020-{split}-{video}"
    scenario_frames: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []
    source_inventory = [_inventory(label_path, source_root)]
    class_counts: Counter[str] = Counter()
    ignored_counts: Counter[str] = Counter()
    track_categories: dict[str, str] = {}

    for index, timestamp_ns, frame, name in zip(
        indices, relative_ns, ordered_frames, names, strict=True
    ):
        image_path = image_directory / name
        _plain_file(image_path, "BDD100K frame")
        width, height = _image_dimensions(image_path)
        source_inventory.append(_inventory(image_path, source_root))
        relative_path = Path(os.path.relpath(image_path, stage / split / video)).as_posix()
        manifest_frame: dict[str, Any] = {
            "frame_index": index,
            "source_timestamp_ns": timestamp_ns,
            "width": width,
            "height": height,
            "path": relative_path,
            "source_frame_index": index,
        }
        if "timestamp" in frame:
            manifest_frame["native_source_timestamp_ms"] = frame["timestamp"]
        scenario_frames.append(manifest_frame)

        seen_tracks: set[str] = set()
        for label_offset, label in enumerate(frame["labels"]):
            context = f"{label_path}:{index}.labels[{label_offset}]"
            identifier = _track_id(label.get("id"), context)
            if identifier in seen_tracks:
                raise ImportFailure(f"{context} duplicates track id {identifier!r} in one frame")
            seen_tracks.add(identifier)
            category = label.get("category")
            if not isinstance(category, str) or category not in CATEGORY_POLICY:
                raise ImportFailure(f"{context} has unsupported category {category!r}")
            previous_category = track_categories.setdefault(identifier, category)
            if previous_category != category:
                raise ImportFailure(
                    f"{context} changes track {identifier!r} from "
                    f"{previous_category!r} to {category!r}"
                )
            attributes = label.get("attributes")
            if not isinstance(attributes, dict):
                raise ImportFailure(f"{context}.attributes must be an object")
            occluded = _boolean_attribute(
                attributes, "occluded", "Occluded", context, required=True
            )
            truncated = _boolean_attribute(
                attributes, "truncated", "Truncated", context, required=True
            )
            crowd = _boolean_attribute(attributes, "crowd", "Crowd", context, required=False)
            class_id, excluded = CATEGORY_POLICY[category]
            ignored = excluded or crowd
            class_counts[class_id] += 1
            if ignored:
                ignored_counts["crowd" if crowd else category] += 1
            truth.append(
                {
                    "schema_version": "cvbench.ground-truth/v1",
                    "target_id": f"bdd100k:{video}:{identifier}",
                    "sequence_id": sequence_id,
                    "source_timestamp_ns": timestamp_ns,
                    "on_screen": True,
                    "eligible_for_detection": not ignored,
                    "visibility_fraction": None,
                    "occlusion": "partial" if occluded else "none",
                    "truncated": truncated,
                    "class_id": class_id,
                    "bbox_xyxy": _box(label, width, height, context),
                    "ignore": ignored,
                    "source_dataset": "BDD100K MOT 2020",
                    "source_category": category,
                    "source_track_id": identifier,
                    "source_frame_index": index,
                    "source_attributes": {
                        "occluded": occluded,
                        "truncated": truncated,
                        "crowd": crowd,
                    },
                }
            )

    truth.sort(key=lambda row: (row["source_timestamp_ns"], row["target_id"]))
    sequence_root = stage / split / video
    sequence_root.mkdir(parents=True)
    truth_path = sequence_root / "ground_truth.jsonl"
    truth_path.write_text("".join(_canonical_json(row) + "\n" for row in truth))
    scenario = {
        "schema_version": "cvbench.scenario/v1",
        "id": sequence_id,
        "family": "bdd100k_mot2020_training",
        "sequence_id": sequence_id,
        "use": "training_only",
        "benchmark_eligible": False,
        "license": "BDD100K dataset terms; see corpus-manifest.json and docs/bdd100k-training-corpus.md",
        "source": "user-supplied official BDD100K MOT 2020 images and Scalabel labels",
        "annotation_scope": "official_annotated_frames_only_no_30fps_interpolation",
        "timestamp_policy": timestamp_policy,
        "mapping_policy_version": MAPPING_POLICY_VERSION,
        "ontology": sorted(class_counts),
        "ground_truth": "ground_truth.jsonl",
        "frames": scenario_frames,
    }
    scenario_path = sequence_root / "scenario.yaml"
    scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False))
    summary = {
        "split": split,
        "video": video,
        "sequence_id": sequence_id,
        "frames": len(scenario_frames),
        "labels": len(truth),
        "class_counts": dict(sorted(class_counts.items())),
        "ignored_counts": dict(sorted(ignored_counts.items())),
        "timestamp_policy": timestamp_policy,
        "source_label": _inventory(label_path, source_root),
        "ground_truth": _inventory(truth_path, stage),
        "scenario": _inventory(scenario_path, stage),
    }
    return summary, source_inventory, [summary["ground_truth"], summary["scenario"]]


def prepare(source_root: Path, output: Path, splits: tuple[str, ...]) -> Path:
    source_root = source_root.resolve()
    output = output.resolve()
    _plain_directory(source_root, "BDD100K source root")
    if output.exists():
        raise ImportFailure(f"output already exists; refusing to overwrite: {output}")
    if output == source_root or source_root in output.parents:
        raise ImportFailure("output must not be inside the source corpus")
    if not splits or any(split not in SPLITS for split in splits) or len(splits) != len(set(splits)):
        raise ImportFailure("splits must be a unique subset of train and val")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        sequences: list[dict[str, Any]] = []
        source_inventory: list[dict[str, Any]] = []
        output_inventory: list[dict[str, Any]] = []
        for split in splits:
            labels_root = source_root / "labels" / "box_track_20" / split
            images_root = source_root / "images" / "track" / split
            _plain_directory(labels_root, f"BDD100K {split} labels")
            _plain_directory(images_root, f"BDD100K {split} images")
            label_files = _strict_children(labels_root, suffix=".json")
            image_directories = _strict_children(images_root, directories=True)
            label_names = {path.stem for path in label_files}
            image_names = {path.name for path in image_directories}
            if label_names != image_names:
                raise ImportFailure(
                    f"{split} sequence inventory differs; "
                    f"labels_only={sorted(label_names - image_names)[:3]}, "
                    f"images_only={sorted(image_names - label_names)[:3]}"
                )
            if not label_files:
                raise ImportFailure(f"BDD100K {split} split is empty")
            image_by_name = {path.name: path for path in image_directories}
            for label_path in label_files:
                summary, source_files, output_files = _sequence(
                    source_root=source_root,
                    stage=stage,
                    split=split,
                    label_path=label_path,
                    image_directory=image_by_name[label_path.stem],
                )
                sequences.append(summary)
                source_inventory.extend(source_files)
                output_inventory.extend(output_files)

        manifest = {
            "schema_version": "cvbench.training-corpus/v1",
            "dataset": "BDD100K MOT 2020",
            "use": "training_only",
            "benchmark_eligible": False,
            "importer_version": IMPORTER_VERSION,
            "importer_sha256": _sha256(Path(__file__)),
            "mapping_policy_version": MAPPING_POLICY_VERSION,
            "category_policy": {
                category: {"class_id": policy[0], "ignore": policy[1]}
                for category, policy in CATEGORY_POLICY.items()
            },
            "timing_policy": {
                "annotated_fps": ANNOTATED_FPS,
                "rule": (
                    "Use exact relative Scalabel timestamp milliseconds when present on every frame; "
                    "otherwise use the official 5 Hz annotated-frame index. Never synthesize 30 FPS truth."
                ),
            },
            "license_notice": LICENSE_NOTICE,
            "provenance": {
                "official_download": "https://bdd-data.berkeley.edu/download.html",
                "official_format": "https://github.com/bdd100k/bdd100k/blob/master/doc/format.md",
                "source_root_name": source_root.name,
                "splits": list(splits),
            },
            "totals": {
                "sequences": len(sequences),
                "frames": sum(sequence["frames"] for sequence in sequences),
                "labels": sum(sequence["labels"] for sequence in sequences),
            },
            "sequences": sequences,
            "source_inventory": sorted(source_inventory, key=lambda item: item["path"]),
            "output_inventory": sorted(output_inventory, key=lambda item: item["path"]),
        }
        (stage / "corpus-manifest.json").write_text(_canonical_json(manifest) + "\n")
        stage.rename(output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output / "corpus-manifest.json"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(".local-ingest/bdd100k/source/bdd100k"),
        help="extracted official bdd100k directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local-ingest/bdd100k/prepared"),
        help="new output directory (must not already exist)",
    )
    parser.add_argument(
        "--split",
        action="append",
        choices=SPLITS,
        dest="splits",
        help="split to prepare; repeat for both (default: train and val)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        manifest = prepare(args.source_root, args.output, tuple(args.splits or SPLITS))
    except ImportFailure as exc:
        print(f"BDD100K import rejected: {exc}", file=sys.stderr)
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
