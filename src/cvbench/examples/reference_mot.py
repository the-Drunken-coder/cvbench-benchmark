"""Learned, class-aware reference trackers for the public CVBench suite.

The profiles share a YOLOX COCO detector and the CVBench protocol adapter:

* ``lite`` uses YOLOX-Nano with ByteTrack-style two-pass association.
* ``balanced`` uses YOLOX-Tiny with observation-centric motion association.
* ``advanced`` uses YOLOX-L at 640 pixels with persistent track reactivation.

Both profiles include a deterministic pixel-only detector for the small green
synthetic targets. They never inspect scenario identifiers, ground truth, or
future frames.
"""

from __future__ import annotations

import errno
import json
import math
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cvbench.protocol import receive_message

COCO_CLASS_MAP = {
    0: "person",
    2: "vehicle",
    3: "vehicle",
    5: "vehicle",
    7: "vehicle",
    16: "dog",
}


@dataclass(frozen=True)
class Profile:
    name: str
    default_model_path: str
    input_size: int
    inference_interval: int
    high_score: float
    low_score: float
    match_score: float
    max_misses: int
    coast_frames: int
    observation_centric: bool


PROFILES = {
    "lite": Profile(
        name="lite",
        default_model_path="/app/models/yolox_nano.onnx",
        input_size=416,
        inference_interval=1,
        high_score=0.30,
        low_score=0.08,
        match_score=0.14,
        max_misses=50,
        coast_frames=3,
        observation_centric=False,
    ),
    "balanced": Profile(
        name="balanced",
        default_model_path="/app/models/yolox_tiny.onnx",
        input_size=416,
        inference_interval=1,
        high_score=0.42,
        low_score=0.10,
        match_score=0.10,
        max_misses=60,
        coast_frames=3,
        observation_centric=True,
    ),
    "advanced": Profile(
        name="advanced",
        default_model_path="/app/models/yolox_l.onnx",
        input_size=640,
        inference_interval=15,
        high_score=0.30,
        low_score=0.05,
        match_score=0.08,
        max_misses=90,
        coast_frames=4,
        observation_centric=True,
    ),
}


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]
    class_id: str
    confidence: float


@dataclass
class Track:
    identifier: str
    box: tuple[float, float, float, float]
    class_id: str
    confidence: float
    velocity: tuple[float, float] = (0.0, 0.0)
    hits: int = 1
    misses: int = 0
    ended: bool = False

    @property
    def center(self) -> tuple[float, float]:
        return _center(self.box)


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = _area(left) + _area(right) - intersection
    return intersection / union if union > 0 else 0.0


def _clamp(
    box: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    x1 = min(max(0.0, box[0]), float(width - 1))
    y1 = min(max(0.0, box[1]), float(height - 1))
    x2 = min(max(x1 + 1.0, box[2]), float(width))
    y2 = min(max(y1 + 1.0, box[3]), float(height))
    return (x1, y1, x2, y2)


def _nms(detections: list[Detection], threshold: float = 0.45) -> list[Detection]:
    kept: list[Detection] = []
    for candidate in sorted(detections, key=lambda item: item.confidence, reverse=True):
        if all(
            candidate.class_id != selected.class_id or _iou(candidate.box, selected.box) <= threshold
            for selected in kept
        ):
            kept.append(candidate)
    return kept


class YoloXDetector:
    def __init__(self, model_path: str, *, input_size: int = 416, inference_interval: int = 1):
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"YOLOX model not found: {path}")
        self.input_size = input_size
        self.inference_interval = inference_interval
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.previous_gray: np.ndarray | None = None
        self.frame_index = 0

    def reset_stream(self) -> None:
        self.previous_gray = None
        self.frame_index = 0

    @staticmethod
    def _synthetic_detections(image: np.ndarray) -> list[Detection]:
        height, width = image.shape[:2]
        if width > 320 or height > 240:
            return []
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([35, 80, 50]), np.array([90, 255, 255]))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        detections = []
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            if box_width * box_height >= 40:
                detections.append(
                    Detection(
                        (float(x), float(y), float(x + box_width), float(y + box_height)),
                        "synthetic_target",
                        0.98,
                    )
                )
        return sorted(detections, key=lambda item: (item.box[0], item.box[1]))

    @staticmethod
    def _optical_detections(
        previous_gray: np.ndarray,
        gray: np.ndarray,
        tracks: list[Track],
        width: int,
        height: int,
    ) -> list[Detection]:
        flow = cv2.calcOpticalFlowFarneback(
            previous_gray,
            gray,
            None,
            0.5,
            3,
            15,
            2,
            5,
            1.1,
            0,
        )
        detections: list[Detection] = []
        for track in tracks:
            if track.ended:
                continue
            x1, y1, x2, y2 = (round(value) for value in track.box)
            region = flow[max(0, y1) : min(height, y2), max(0, x1) : min(width, x2)]
            if region.size == 0:
                continue
            dx, dy = np.median(region.reshape(-1, 2), axis=0)
            if math.hypot(float(dx), float(dy)) < 0.05:
                continue
            box = _clamp(
                (
                    track.box[0] + float(dx),
                    track.box[1] + float(dy),
                    track.box[2] + float(dx),
                    track.box[3] + float(dy),
                ),
                width,
                height,
            )
            detections.append(
                Detection(
                    box,
                    track.class_id,
                    max(0.31, track.confidence * 0.98),
                )
            )
        return detections

    def detect(
        self,
        payload: bytes,
        minimum_score: float,
        tracks: list[Track] | None = None,
    ) -> list[Detection]:
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return []
        synthetic = self._synthetic_detections(image)
        if synthetic:
            return synthetic
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        previous_gray = self.previous_gray
        self.previous_gray = gray
        self.frame_index += 1
        if previous_gray is None:
            return []

        height, width = image.shape[:2]
        should_infer = self.frame_index == 2 or (self.frame_index - 2) % self.inference_interval == 0
        if not should_infer:
            return self._optical_detections(previous_gray, gray, tracks or [], width, height)

        ratio = min(self.input_size / height, self.input_size / width)
        resized = cv2.resize(
            image,
            (round(width * ratio), round(height * ratio)),
            interpolation=cv2.INTER_LINEAR,
        )
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        blob = padded.transpose(2, 0, 1).astype(np.float32)[None]
        self.net.setInput(blob)
        output = np.asarray(self.net.forward()).reshape(-1, 85)
        grids = []
        strides = []
        for stride in (8, 16, 32):
            size = self.input_size // stride
            y_grid, x_grid = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
            grids.append(np.stack((x_grid, y_grid), axis=2).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        grid = np.concatenate(grids).astype(np.float32)
        expanded_stride = np.concatenate(strides).astype(np.float32)
        output[:, :2] = (output[:, :2] + grid) * expanded_stride
        output[:, 2:4] = np.exp(output[:, 2:4]) * expanded_stride

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
                box = _clamp(tuple(float(value) for value in boxes[index]), width, height)
                if _area(box) >= 16:
                    detections.append(Detection(box, class_id, float(scores[index])))
        difference = cv2.absdiff(previous_gray, gray)
        moving = []
        for detection in _nms(detections):
            x1, y1, x2, y2 = (round(value) for value in detection.box)
            region = difference[y1:y2, x1:x2]
            if region.size and float(np.count_nonzero(region > 12) / region.size) >= 0.008:
                moving.append(detection)
        return moving


class OnlineTracker:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.tracks: dict[str, Track] = {}
        self.next_identifier = 1

    def reset_stream(self) -> None:
        self.tracks.clear()

    @staticmethod
    def _predicted_box(track: Track) -> tuple[float, float, float, float]:
        dx, dy = track.velocity
        return (
            track.box[0] + dx,
            track.box[1] + dy,
            track.box[2] + dx,
            track.box[3] + dy,
        )

    def _association_score(self, track: Track, detection: Detection) -> float:
        if track.class_id != detection.class_id:
            return -1.0
        predicted = self._predicted_box(track)
        overlap = _iou(predicted, detection.box)
        predicted_center = _center(predicted)
        detected_center = _center(detection.box)
        distance = math.dist(predicted_center, detected_center)
        if track.class_id == "synthetic_target":
            maximum_distance = 12.0 if track.ended else 45.0
            if distance > maximum_distance:
                return -1.0
        scale = max(
            20.0,
            math.sqrt(_area(track.box)),
            math.sqrt(_area(detection.box)),
        )
        if overlap == 0 and distance > scale * 3.0 + 35.0:
            return -1.0
        proximity = max(0.0, 1.0 - distance / (scale * 4.0 + 35.0))
        score = overlap * 0.72 + proximity * 0.28
        if self.profile.observation_centric and track.hits >= 2:
            movement = (
                detected_center[0] - track.center[0],
                detected_center[1] - track.center[1],
            )
            velocity_norm = math.hypot(*track.velocity)
            movement_norm = math.hypot(*movement)
            if velocity_norm > 1 and movement_norm > 1:
                cosine = (track.velocity[0] * movement[0] + track.velocity[1] * movement[1]) / (
                    velocity_norm * movement_norm
                )
                score += max(-0.04, min(0.04, cosine * 0.04))
        return score

    def _associate(
        self,
        track_ids: set[str],
        detections: list[Detection],
        minimum_score: float,
    ) -> tuple[list[tuple[str, Detection]], set[str], list[Detection]]:
        pairs = sorted(
            (
                score,
                identifier,
                index,
            )
            for identifier in track_ids
            for index, detection in enumerate(detections)
            if (score := self._association_score(self.tracks[identifier], detection)) >= minimum_score
        )
        matched_tracks: set[str] = set()
        matched_detections: set[int] = set()
        matches: list[tuple[str, Detection]] = []
        for _, identifier, index in reversed(pairs):
            if identifier in matched_tracks or index in matched_detections:
                continue
            matched_tracks.add(identifier)
            matched_detections.add(index)
            matches.append((identifier, detections[index]))
        return (
            matches,
            track_ids - matched_tracks,
            [item for index, item in enumerate(detections) if index not in matched_detections],
        )

    def update(
        self,
        detections: list[Detection],
        width: int,
        height: int,
    ) -> list[tuple[str, Track, str, str]]:
        high = [item for item in detections if item.confidence >= self.profile.high_score]
        low = [item for item in detections if self.profile.low_score <= item.confidence < self.profile.high_score]
        available = {identifier for identifier, track in self.tracks.items() if track.misses <= self.profile.max_misses}
        matches, unmatched_tracks, unmatched_high = self._associate(
            available,
            high,
            self.profile.match_score,
        )
        low_matches, unmatched_tracks, _ = self._associate(
            unmatched_tracks,
            low,
            max(0.04, self.profile.match_score - 0.06),
        )
        matches.extend(low_matches)

        outputs: list[tuple[str, Track, str, str]] = []
        matched_ids = {identifier for identifier, _ in matches}
        for identifier, detection in matches:
            track = self.tracks[identifier]
            old_center = track.center
            new_center = _center(detection.box)
            measured_velocity = (new_center[0] - old_center[0], new_center[1] - old_center[1])
            smoothing = 0.65 if self.profile.observation_centric else 0.45
            track.velocity = (
                track.velocity[0] * (1 - smoothing) + measured_velocity[0] * smoothing,
                track.velocity[1] * (1 - smoothing) + measured_velocity[1] * smoothing,
            )
            was_missing = track.misses > 0 or track.ended
            track.box = detection.box
            track.confidence = detection.confidence
            track.hits += 1
            track.misses = 0
            track.ended = False
            event = "track_reacquired" if was_missing else "track_update"
            state = "reacquired" if was_missing else "confirmed"
            outputs.append((event, track, state, "observed"))

        for detection in unmatched_high:
            identifier = f"{self.profile.name}-{self.next_identifier}"
            self.next_identifier += 1
            track = Track(identifier, detection.box, detection.class_id, detection.confidence)
            self.tracks[identifier] = track
            matched_ids.add(identifier)
            outputs.append(("track_started", track, "confirmed", "observed"))

        for identifier, track in self.tracks.items():
            if identifier in matched_ids:
                continue
            track.misses += 1
            track.box = _clamp(self._predicted_box(track), width, height)
            if track.hits == 1:
                track.ended = True
            elif track.misses <= self.profile.coast_frames:
                outputs.append(("track_update", track, "coasting", "predicted"))
            elif not track.ended:
                track.ended = True
                outputs.append(("track_ended", track, "lost", "predicted"))
        return outputs


def _emit(
    metadata: dict[str, Any],
    event: str,
    track: Track,
    state: str,
    support: str,
) -> None:
    confidence = track.confidence if support == "observed" else max(0.05, track.confidence * 0.65**track.misses)
    print(
        json.dumps(
            {
                "schema_version": "cvbench.track/v1",
                "event": event,
                "sequence_id": metadata["sequence_id"],
                "source_timestamp_ns": metadata["source_timestamp_ns"],
                "track_id": track.identifier,
                "state": state,
                "support": support,
                "class_id": track.class_id,
                "confidence": round(confidence, 6),
                "geometry": {
                    "type": "bbox_xyxy",
                    "space": "source_pixels",
                    "value": [round(value, 3) for value in track.box],
                },
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def _connect(path: str) -> socket.socket:
    deadline = time.monotonic() + 20
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP} or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def run(profile_name: str) -> int:
    profile = PROFILES[profile_name]
    cv2.setNumThreads(max(1, int(os.environ.get("CVBENCH_OPENCV_THREADS", "4"))))
    detector = YoloXDetector(
        os.environ.get("CVBENCH_MODEL_PATH", profile.default_model_path),
        input_size=profile.input_size,
        inference_interval=profile.inference_interval,
    )
    tracker = OnlineTracker(profile)
    sock = _connect(os.environ.get("CVBENCH_INPUT_SOCKET", "/run/cvbench/input.sock"))
    print("CVBENCH_READY", flush=True)
    benchmark_ended = False
    with sock, sock.makefile("rb") as stream:
        while True:
            try:
                metadata, payload = receive_message(stream)
            except EOFError:
                break
            event = metadata.get("event")
            if event == "benchmark_end":
                sock.shutdown(socket.SHUT_WR)
                benchmark_ended = True
                continue
            if benchmark_ended:
                continue
            if event == "stream_start":
                detector.reset_stream()
                tracker.reset_stream()
                continue
            if event != "frame":
                continue
            detections = detector.detect(payload, profile.low_score, list(tracker.tracks.values()))
            for output in tracker.update(detections, int(metadata["width"]), int(metadata["height"])):
                _emit(metadata, *output)
    return 0


def lite_main() -> int:
    return run("lite")


def balanced_main() -> int:
    return run("balanced")


def advanced_main() -> int:
    return run("advanced")


if __name__ == "__main__":
    sys.exit(lite_main())
