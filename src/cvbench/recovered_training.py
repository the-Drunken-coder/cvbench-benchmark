"""Build a training-only pseudo-labeled corpus from the recovered clean videos."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

CORPUS_SCHEMA = "cvbench.training-corpus/v1"
SAMPLE_SCHEMA = "cvbench.training-sample/v1"
ANNOTATION_SCHEMA = "cvbench.pseudo-detection/v1"
MODEL_ID = "Megvii YOLOX-X COCO 640 ONNX"
MODEL_SHA256 = "c892d7aaf1c4746d8a4d675bec669a4db4f434b4ee1efb654bc9b353379c7c55"
CLASS_IDS = {"person": 0, "dog": 1}
COCO_CLASS_MAP = {0: "person", 16: "dog"}
VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4"}
VISUAL_AUDIT_EXCLUSIONS = (
    {
        "clip_id": "pixabay-212474-forest-walk",
        "class_id": "person",
        "normalized_center_region": [0.68, 0.54, 0.77, 0.72],
        "reason": "static tree root repeatedly detected as a second person",
    },
)


@dataclass(frozen=True)
class SourceClip:
    id: str
    filename: str
    sha256: str
    split: str
    source_url: str
    creator: str
    license_name: str
    license_url: str


SOURCE_CLIPS = (
    SourceClip(
        id="pexels-18187166-dune",
        filename="pexels-18187166-huacachina-sand-dune-4k.mp4",
        sha256="05bc3794c11d787e946d868f6d5e2d1a40b0939dc6218aa3263b138adc64e0cc",
        split="validation",
        source_url="https://www.pexels.com/video/a-man-walking-up-a-sand-dune-in-the-desert-18187166/",
        creator="Florian Delee",
        license_name="Pexels License",
        license_url="https://www.pexels.com/license/",
    ),
    SourceClip(
        id="pixabay-112059-dog-road",
        filename="pixabay-112059-young-woman-road-dog-4k.mp4",
        sha256="b9fdd57c97d629552dcdb6063a88827a27dfa240834d39cd47dc992207842c1b",
        split="training",
        source_url="https://pixabay.com/videos/young-woman-road-walk-in-the-woods-112059/",
        creator="RuslanSikunov",
        license_name="Pixabay Content License",
        license_url="https://pixabay.com/service/license-summary/",
    ),
    SourceClip(
        id="pixabay-145851-forest-bench",
        filename="pixabay-145851-man-trees-nature-bench-4k.mp4",
        sha256="b22382b1e94ca5189ff76a9ce37dd71660a36a785d0b50f897a23db363b083d9",
        split="training",
        source_url="https://pixabay.com/videos/man-trees-nature-forest-145851/",
        creator="Matias_Luge",
        license_name="Pixabay Content License",
        license_url="https://pixabay.com/service/license-summary/",
    ),
    SourceClip(
        id="pixabay-212474-forest-walk",
        filename="pixabay-212474-man-walking-forest-4k.mp4",
        sha256="1f0b743c9b2af9fefe8c6dae05adb613b24b59c94f8bf2319d726eaee720177a",
        split="training",
        source_url="https://pixabay.com/videos/man-walking-forest-alone-trees-212474/",
        creator="Matthias_Groeneveld",
        license_name="Pixabay Content License",
        license_url="https://pixabay.com/service/license-summary/",
    ),
    SourceClip(
        id="pixabay-28855-ravine",
        filename="pixabay-28855-autumn-forest-tourists.mp4",
        sha256="825d9707b99ad212efc94e0dda630ba9253239b404c960002c7b385386e69641",
        split="training",
        source_url="https://pixabay.com/videos/autumn-forest-tourists-walk-stream-28855/",
        creator="spoot (Alex Kuimov)",
        license_name="Pixabay Content License",
        license_url="https://pixabay.com/service/license-summary/",
    ),
)


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]
    class_id: str
    confidence: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = _area(left) + _area(right) - intersection
    return intersection / union if union > 0 else 0.0


def _nms(detections: Iterable[Detection], threshold: float = 0.45) -> list[Detection]:
    kept: list[Detection] = []
    for candidate in sorted(detections, key=lambda item: item.confidence, reverse=True):
        if all(
            candidate.class_id != selected.class_id or _iou(candidate.box, selected.box) <= threshold
            for selected in kept
        ):
            kept.append(candidate)
    return sorted(kept, key=lambda item: (item.class_id, item.box))


def sample_frame_indices(frame_count: int, source_fps: float, sample_fps: float) -> list[int]:
    """Return deterministic nearest-frame samples without duplicating source frames."""
    if frame_count <= 0 or source_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame_count and frame rates must be positive")
    count = max(1, int(np.ceil(frame_count * sample_fps / source_fps)))
    return sorted({min(frame_count - 1, round(index * source_fps / sample_fps)) for index in range(count)})


class YoloXDetector:
    def __init__(self, model: Path, *, input_size: int = 640) -> None:
        if _sha256(model) != MODEL_SHA256:
            raise ValueError(f"unexpected YOLOX-X model bytes: {model}")
        self.input_size = input_size
        self.net = cv2.dnn.readNetFromONNX(str(model))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        grids = []
        strides = []
        for stride in (8, 16, 32):
            size = input_size // stride
            y_grid, x_grid = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
            grids.append(np.stack((x_grid, y_grid), axis=2).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.expanded_stride = np.concatenate(strides).astype(np.float32)

    def detect(self, image: np.ndarray, minimum_score: float) -> list[Detection]:
        height, width = image.shape[:2]
        ratio = min(self.input_size / height, self.input_size / width)
        resized = cv2.resize(
            image,
            (round(width * ratio), round(height * ratio)),
            interpolation=cv2.INTER_LINEAR,
        )
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        self.net.setInput(padded.transpose(2, 0, 1).astype(np.float32)[None])
        output = np.asarray(self.net.forward()).reshape(-1, 85)
        output[:, :2] = (output[:, :2] + self.grid) * self.expanded_stride
        output[:, 2:4] = np.exp(output[:, 2:4]) * self.expanded_stride

        boxes = output[:, :4].copy()
        boxes[:, 0] = output[:, 0] - output[:, 2] / 2
        boxes[:, 1] = output[:, 1] - output[:, 3] / 2
        boxes[:, 2] = output[:, 0] + output[:, 2] / 2
        boxes[:, 3] = output[:, 1] + output[:, 3] / 2
        boxes /= ratio
        class_scores = output[:, 4:5] * output[:, 5:]

        detections: list[Detection] = []
        for coco_id, class_id in COCO_CLASS_MAP.items():
            scores = class_scores[:, coco_id]
            for index in np.flatnonzero(scores >= minimum_score):
                raw = boxes[index]
                box = (
                    min(max(0.0, float(raw[0])), float(width - 1)),
                    min(max(0.0, float(raw[1])), float(height - 1)),
                    min(max(1.0, float(raw[2])), float(width)),
                    min(max(1.0, float(raw[3])), float(height)),
                )
                box = (box[0], box[1], max(box[0] + 1.0, box[2]), max(box[1] + 1.0, box[3]))
                if box[2] <= width and box[3] <= height and _area(box) >= 16:
                    detections.append(Detection(box, class_id, float(scores[index])))
        return _nms(detections)


def _validate_sources(source: Path) -> dict[str, Path]:
    expected = {clip.filename: clip for clip in SOURCE_CLIPS}
    actual_videos = {path.name for path in source.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES}
    if actual_videos != set(expected):
        missing = sorted(set(expected) - actual_videos)
        extra = sorted(actual_videos - set(expected))
        raise ValueError(f"source video inventory mismatch; missing={missing}, extra={extra}")
    paths = {name: source / name for name in expected}
    for name, path in paths.items():
        if _sha256(path) != expected[name].sha256:
            raise ValueError(f"source video hash mismatch: {path}")
    return paths


def _resize(image: np.ndarray, maximum_dimension: int) -> tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    scale = min(1.0, maximum_dimension / max(width, height))
    if scale == 1.0:
        return image, scale
    return (
        cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA),
        scale,
    )


def _excluded_by_visual_audit(clip_id: str, detection: Detection, width: int, height: int) -> bool:
    center_x = (detection.box[0] + detection.box[2]) / (2 * width)
    center_y = (detection.box[1] + detection.box[3]) / (2 * height)
    return any(
        exclusion["clip_id"] == clip_id
        and exclusion["class_id"] == detection.class_id
        and exclusion["normalized_center_region"][0] <= center_x <= exclusion["normalized_center_region"][2]
        and exclusion["normalized_center_region"][1] <= center_y <= exclusion["normalized_center_region"][3]
        for exclusion in VISUAL_AUDIT_EXCLUSIONS
    )


def _yolo_lines(detections: list[Detection], width: int, height: int) -> list[str]:
    lines = []
    for detection in detections:
        x1, y1, x2, y2 = detection.box
        center_x = (x1 + x2) / (2 * width)
        center_y = (y1 + y2) / (2 * height)
        box_width = (x2 - x1) / width
        box_height = (y2 - y1) / height
        lines.append(
            f"{CLASS_IDS[detection.class_id]} {center_x:.8f} {center_y:.8f} "
            f"{box_width:.8f} {box_height:.8f}"
        )
    return lines


def _write_contact_sheets(
    root: Path,
    clip_id: str,
    samples: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    review = root / "review"
    review.mkdir(exist_ok=True)
    by_image: dict[str, list[dict[str, Any]]] = {}
    for annotation in annotations:
        by_image.setdefault(annotation["image"], []).append(annotation)
    ledger = []
    colors = {"person": (60, 220, 60), "vehicle": (0, 190, 255), "dog": (255, 120, 40)}
    for sheet_index, start in enumerate(range(0, len(samples), 25), 1):
        selected = samples[start : start + 25]
        canvas = np.full((1000, 1600, 3), 28, dtype=np.uint8)
        for cell_index, sample in enumerate(selected):
            image = cv2.imread(str(root / sample["image"]))
            if image is None:
                raise ValueError(f"cannot read generated review image: {sample['image']}")
            for annotation in by_image.get(sample["image"], []):
                x1, y1, x2, y2 = (round(value) for value in annotation["bbox_xyxy"])
                color = colors[annotation["class_id"]]
                cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
                cv2.putText(
                    image,
                    f"{annotation['class_id']} {annotation['confidence']:.2f}",
                    (x1, max(18, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    color,
                    2,
                    cv2.LINE_AA,
                )
            thumbnail = cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA)
            row, column = divmod(cell_index, 5)
            y, x = row * 200, column * 320
            canvas[y : y + 180, x : x + 320] = thumbnail
            caption = f"{sample['source_frame_index']} | {sample['detection_count']} labels"
            cv2.putText(canvas, caption, (x + 5, y + 195), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (235, 235, 235), 1)
        relative = f"review/{clip_id}-{sheet_index:02d}.jpg"
        if not cv2.imwrite(str(root / relative), canvas, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise ValueError(f"cannot write contact sheet: {relative}")
        ledger.append({"path": relative, "samples": [sample["image"] for sample in selected]})
    return ledger


def _write_inventory(root: Path) -> None:
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "inventory.sha256")
    body = "".join(f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in paths)
    (root / "inventory.sha256").write_text(body)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verify_inventory(root: Path) -> None:
    inventory = root / "inventory.sha256"
    if not inventory.is_file() or inventory.is_symlink():
        raise ValueError("corpus inventory hash manifest is missing or unsafe")
    expected = inventory.read_text().splitlines()
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "inventory.sha256")
    if any(path.is_symlink() for path in paths):
        raise ValueError("corpus inventory cannot contain symlinks")
    actual = [f"{_sha256(path)}  {path.relative_to(root).as_posix()}" for path in paths]
    if expected != actual:
        raise ValueError("corpus inventory hash manifest does not match")


def verify_corpus(root: Path) -> dict[str, Any]:
    manifest = yaml.safe_load((root / "corpus.yaml").read_text())
    if manifest.get("schema_version") != CORPUS_SCHEMA:
        raise ValueError("invalid recovered training corpus schema")
    if manifest.get("data_role") != "model_training_only" or manifest.get("evaluation_eligible") is not False:
        raise ValueError("recovered corpus must remain training-only and evaluation-ineligible")
    if manifest.get("annotation_scope") != "machine_generated_non_exhaustive_object_detections":
        raise ValueError("recovered corpus must not claim exhaustive ground truth")
    verify_inventory(root)

    samples = _read_jsonl(root / "samples.jsonl")
    annotations = _read_jsonl(root / "annotations.jsonl")
    by_image: dict[str, list[dict[str, Any]]] = {}
    for annotation in annotations:
        by_image.setdefault(annotation["image"], []).append(annotation)
    for sample in samples:
        image_path = root / sample["image"]
        label_path = root / sample["label"]
        if not image_path.is_file() or not label_path.is_file():
            raise ValueError(f"missing sample asset: {sample['image']}")
        image = cv2.imread(str(image_path))
        if image is None or image.shape[1] != sample["width"] or image.shape[0] != sample["height"]:
            raise ValueError(f"sample dimensions do not match: {sample['image']}")
        detections = by_image.get(sample["image"], [])
        if len(detections) != sample["detection_count"]:
            raise ValueError(f"sample detection count does not match: {sample['image']}")
        if len(label_path.read_text().splitlines()) != len(detections):
            raise ValueError(f"YOLO label count does not match: {sample['label']}")
        for detection in detections:
            x1, y1, x2, y2 = detection["bbox_xyxy"]
            if not (0 <= x1 < x2 <= sample["width"] and 0 <= y1 < y2 <= sample["height"]):
                raise ValueError(f"annotation outside image: {sample['image']}")

    for source in manifest["sources"]:
        video = root / source["local_video"]
        if _sha256(video) != source["sha256"]:
            raise ValueError(f"local source video hash mismatch: {video}")

    expected_training = [
        sample["image"] for sample in samples if sample["split"] == "training" and sample["detection_count"]
    ]
    expected_validation = [
        sample["image"] for sample in samples if sample["split"] == "validation" and sample["detection_count"]
    ]
    if (root / "train.txt").read_text().splitlines() != expected_training:
        raise ValueError("training image list must contain only positive pseudo-labeled samples")
    if (root / "validation.txt").read_text().splitlines() != expected_validation:
        raise ValueError("validation image list must contain only positive pseudo-labeled samples")

    review_ledger = json.loads((root / manifest["review"]["ledger"]).read_text())
    reviewed_images = [image for sheet in review_ledger for image in sheet["samples"]]
    sample_images = [sample["image"] for sample in samples]
    if reviewed_images != sample_images or len(reviewed_images) != len(set(reviewed_images)):
        raise ValueError("review ledger must cover every sampled frame exactly once")

    return {"samples": len(samples), "annotations": len(annotations), "clips": len(manifest["sources"])}


def build_corpus(
    source: Path,
    output: Path,
    model: Path,
    runtime_id: str,
    *,
    sample_fps: float,
    maximum_dimension: int,
    confidence_threshold: float,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError(f"output already exists; refusing to replace it: {output}")
    if not runtime_id.startswith("sha256:") or len(runtime_id) != 71:
        raise ValueError("runtime-id must be an immutable sha256 Docker image ID")
    sources = _validate_sources(source)
    detector = YoloXDetector(model)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "videos").mkdir()
        (temporary / "images").mkdir()
        (temporary / "labels").mkdir()
        all_samples: list[dict[str, Any]] = []
        all_annotations: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        review_sheets: list[dict[str, Any]] = []
        cv2.setNumThreads(4)

        for clip in SOURCE_CLIPS:
            print(f"labeling {clip.id}...", flush=True)
            source_path = sources[clip.filename]
            video_path = temporary / "videos" / clip.filename
            shutil.copyfile(source_path, video_path)
            capture = cv2.VideoCapture(str(source_path))
            if not capture.isOpened():
                raise ValueError(f"cannot decode source video: {source_path}")
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            reported_count = round(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            source_width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            source_height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if fps <= 0:
                raise ValueError(f"invalid source frame rate: {source_path}")
            target_indices = set(sample_frame_indices(reported_count, fps, sample_fps)) if reported_count > 0 else set()
            clip_samples: list[dict[str, Any]] = []
            clip_annotations: list[dict[str, Any]] = []
            excluded_proposal_count = 0
            decoded_count = 0
            next_sample = 0
            while True:
                ok, source_image = capture.read()
                if not ok:
                    break
                frame_index = decoded_count
                decoded_count += 1
                should_sample = (
                    frame_index in target_indices
                    if target_indices
                    else frame_index == round(next_sample * fps / sample_fps)
                )
                if not should_sample:
                    continue
                next_sample += 1
                image, scale = _resize(source_image, maximum_dimension)
                raw_detections = detector.detect(image, confidence_threshold)
                detections = [
                    detection
                    for detection in raw_detections
                    if not _excluded_by_visual_audit(clip.id, detection, image.shape[1], image.shape[0])
                ]
                excluded_proposal_count += len(raw_detections) - len(detections)
                stem = f"{clip.id}-frame-{frame_index:06d}"
                image_relative = f"images/{stem}.jpg"
                label_relative = f"labels/{stem}.txt"
                if not cv2.imwrite(
                    str(temporary / image_relative), image, [cv2.IMWRITE_JPEG_QUALITY, 92]
                ):
                    raise ValueError(f"cannot write training image: {image_relative}")
                (temporary / label_relative).write_text(
                    "\n".join(_yolo_lines(detections, image.shape[1], image.shape[0]))
                    + ("\n" if detections else "")
                )
                sample = {
                    "schema_version": SAMPLE_SCHEMA,
                    "clip_id": clip.id,
                    "split": clip.split,
                    "image": image_relative,
                    "label": label_relative,
                    "source_frame_index": frame_index,
                    "source_timestamp_ns": round(frame_index * 1_000_000_000 / fps),
                    "width": image.shape[1],
                    "height": image.shape[0],
                    "detection_count": len(detections),
                    "annotation_state": "model_generated_with_visual_corrections",
                }
                clip_samples.append(sample)
                if len(clip_samples) % 25 == 0:
                    print(f"  {clip.id}: {len(clip_samples)} sampled frames", flush=True)
                for detection in detections:
                    x1, y1, x2, y2 = detection.box
                    annotation = {
                        "schema_version": ANNOTATION_SCHEMA,
                        "clip_id": clip.id,
                        "image": image_relative,
                        "source_frame_index": frame_index,
                        "source_timestamp_ns": sample["source_timestamp_ns"],
                        "class_id": detection.class_id,
                        "bbox_xyxy": [round(value, 3) for value in detection.box],
                        "source_bbox_xyxy": [round(value / scale, 3) for value in detection.box],
                        "confidence": round(detection.confidence, 6),
                        "labeler_model": MODEL_ID,
                    }
                    clip_annotations.append(annotation)
            capture.release()
            if decoded_count <= 0 or not clip_samples:
                raise ValueError(f"source video yielded no training samples: {source_path}")
            source_record = asdict(clip)
            source_record.update(
                {
                    "local_video": f"videos/{clip.filename}",
                    "width": source_width,
                    "height": source_height,
                    "native_fps": fps,
                    "decoded_frame_count": decoded_count,
                    "duration_seconds": decoded_count / fps,
                    "sample_count": len(clip_samples),
                    "annotation_count": len(clip_annotations),
                    "excluded_proposal_count": excluded_proposal_count,
                }
            )
            source_records.append(source_record)
            print(
                f"  {clip.id}: complete ({len(clip_samples)} frames, {len(clip_annotations)} proposals)",
                flush=True,
            )
            all_samples.extend(clip_samples)
            all_annotations.extend(clip_annotations)
            review_sheets.extend(_write_contact_sheets(temporary, clip.id, clip_samples, clip_annotations))

        (temporary / "samples.jsonl").write_text("".join(_json_line(row) + "\n" for row in all_samples))
        (temporary / "annotations.jsonl").write_text("".join(_json_line(row) + "\n" for row in all_annotations))
        train_images = [
            row["image"] for row in all_samples if row["split"] == "training" and row["detection_count"]
        ]
        validation_images = [
            row["image"] for row in all_samples if row["split"] == "validation" and row["detection_count"]
        ]
        (temporary / "train.txt").write_text("".join(path + "\n" for path in train_images))
        (temporary / "validation.txt").write_text("".join(path + "\n" for path in validation_images))
        (temporary / "dataset.yaml").write_text(
            yaml.safe_dump(
                {
                    "path": ".",
                    "train": "train.txt",
                    "val": "validation.txt",
                    "names": {value: key for key, value in CLASS_IDS.items()},
                },
                sort_keys=False,
            )
        )
        class_counts = {
            class_id: sum(row["class_id"] == class_id for row in all_annotations) for class_id in CLASS_IDS
        }
        confidences = [row["confidence"] for row in all_annotations]
        manifest = {
            "schema_version": CORPUS_SCHEMA,
            "id": "recovered-clean-videos-pseudolabels-v1",
            "data_role": "model_training_only",
            "evaluation_eligible": False,
            "task": "object_detection",
            "annotation_scope": "machine_generated_non_exhaustive_object_detections",
            "annotation_status": "model_generated_with_visual_corrections",
            "unknown_is_background": False,
            "native_media_preserved": True,
            "sample_fps": sample_fps,
            "maximum_training_image_dimension": maximum_dimension,
            "ontology": list(CLASS_IDS),
            "labeler": {
                "model": MODEL_ID,
                "model_sha256": MODEL_SHA256,
                "runtime_image_id": runtime_id,
                "opencv_version": cv2.__version__,
                "input_size": 640,
                "confidence_threshold": confidence_threshold,
                "nms_iou_threshold": 0.45,
            },
            "summary": {
                "unique_videos": len(source_records),
                "sample_count": len(all_samples),
                "annotation_count": len(all_annotations),
                "positive_training_sample_count": len(train_images),
                "positive_validation_sample_count": len(validation_images),
                "class_counts": class_counts,
                "empty_sample_count": sum(row["detection_count"] == 0 for row in all_samples),
                "confidence_min": min(confidences) if confidences else None,
                "confidence_median": statistics.median(confidences) if confidences else None,
                "confidence_max": max(confidences) if confidences else None,
            },
            "sources": source_records,
            "review": {
                "status": "visual_audit_complete",
                "coverage": "every sampled frame appears once in a contact sheet",
                "ledger": "review/ledger.json",
                "sheets": len(review_sheets),
                "corrections": list(VISUAL_AUDIT_EXCLUSIONS),
            },
        }
        (temporary / "corpus.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, width=120))
        (temporary / "review" / "ledger.json").write_text(json.dumps(review_sheets, indent=2, sort_keys=True) + "\n")
        _write_inventory(temporary)
        summary = verify_corpus(temporary)
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--runtime-id")
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--maximum-dimension", type=int, default=1280)
    parser.add_argument("--confidence-threshold", type=float, default=0.25)
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output_dir.resolve()
    if args.verify_only:
        summary = verify_corpus(output)
    else:
        if args.source_dir is None or args.model is None or args.runtime_id is None:
            raise SystemExit("--source-dir, --model, and --runtime-id are required unless --verify-only is used")
        summary = build_corpus(
            args.source_dir.resolve(),
            output,
            args.model.resolve(),
            args.runtime_id,
            sample_fps=args.sample_fps,
            maximum_dimension=args.maximum_dimension,
            confidence_threshold=args.confidence_threshold,
        )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
