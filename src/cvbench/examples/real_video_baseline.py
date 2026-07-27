"""Small model-free, class-aware motion baseline for real-video-v2.

It deliberately uses only the current JPEG and the immediately preceding
grayscale frame.  It has no scenario map, query box, labels, or ground-truth
access.  The baseline is a classical foreground detector plus nearest-centre
track association, intended to make real-video weaknesses measurable without
adding a model-weight download to CVBench.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from cvbench.protocol import receive_message


@dataclass
class Track:
    identifier: str
    box: list[float]
    class_id: str
    velocity: tuple[float, float] = (0.0, 0.0)
    misses: int = 0
    hits: int = 1
    was_missing: bool = False
    ended: bool = False

    @property
    def center(self) -> tuple[float, float]:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


def _lifecycle_event(*, created: bool, was_missing: bool) -> str:
    return "track_started" if created else ("track_reacquired" if was_missing else "track_update")


def _detections(payload: bytes, previous: np.ndarray | None) -> tuple[list[tuple[list[float], str]], np.ndarray | None]:
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return [], previous
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if previous is None:
        mask = cv2.inRange(gray, 180, 255)
    else:
        delta = cv2.absdiff(gray, previous)
        _, mask = cv2.threshold(delta, 18, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.dilate(mask, np.ones((7, 7), np.uint8), iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    detections: list[tuple[list[float], str]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width >= 12 and height >= 12 and width * height >= 250:
            box = [float(x), float(y), float(x + width), float(y + height)]
            class_id = "person" if height >= width * 1.15 else "vehicle"
            detections.append((box, class_id))
    return sorted(
        detections,
        key=lambda item: (-(item[0][2] - item[0][0]) * (item[0][3] - item[0][1]), item[0][0], item[0][1]),
    )[:16], gray


def _clamp(box: list[float], width: int, height: int) -> list[float]:
    x1 = min(max(0.0, box[0]), float(width - 1))
    y1 = min(max(0.0, box[1]), float(height - 1))
    x2 = min(max(x1 + 1.0, box[2]), float(width))
    y2 = min(max(y1 + 1.0, box[3]), float(height))
    return [x1, y1, x2, y2]


def _emit(metadata: dict[str, Any], track: Track, state: str, support: str, event: str) -> None:
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
                "confidence": 0.55 if support == "observed" else 0.25,
                "geometry": {"type": "bbox_xyxy", "space": "source_pixels", "value": track.box},
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


def main() -> int:
    sock = _connect(os.environ.get("CVBENCH_INPUT_SOCKET", "/run/cvbench/input.sock"))
    print("CVBENCH_READY", flush=True)
    previous: np.ndarray | None = None
    tracks: dict[str, Track] = {}
    next_id = 1
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
                previous = None
                tracks.clear()
                continue
            if event != "frame":
                continue
            detections, previous = _detections(payload, previous)
            available = set(tracks)
            matched: set[str] = set()
            for box, class_id in detections:
                center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                candidates = sorted(
                    (
                        (track.center[0] + track.velocity[0] - center[0]) ** 2
                        + (track.center[1] + track.velocity[1] - center[1]) ** 2,
                        identifier,
                    )
                    for identifier, track in tracks.items()
                    if identifier in available and track.class_id == class_id
                )
                created = False
                if candidates and candidates[0][0] <= 180**2:
                    identifier = candidates[0][1]
                    available.remove(identifier)
                else:
                    identifier = f"motion-{next_id}"
                    next_id += 1
                    tracks[identifier] = Track(identifier, box, class_id)
                    created = True
                track = tracks[identifier]
                was_missing = track.was_missing
                old_center = track.center
                track.velocity = (center[0] - old_center[0], center[1] - old_center[1])
                track.box = box
                track.hits += 1
                track.misses = 0
                event_name = _lifecycle_event(created=created, was_missing=was_missing)
                track.was_missing = False
                track.ended = False
                matched.add(identifier)
                state = "reacquired" if event_name == "track_reacquired" else "confirmed"
                _emit(metadata, track, state, "observed", event_name)
            for identifier, track in list(tracks.items()):
                if identifier in matched:
                    continue
                track.misses += 1
                track.was_missing = True
                track.box = _clamp(
                    [
                        track.box[0] + track.velocity[0],
                        track.box[1] + track.velocity[1],
                        track.box[2] + track.velocity[0],
                        track.box[3] + track.velocity[1],
                    ],
                    int(metadata["width"]),
                    int(metadata["height"]),
                )
                if track.misses <= 4:
                    _emit(metadata, track, "coasting", "predicted", "track_update")
                elif not track.ended:
                    track.ended = True
                    _emit(metadata, track, "lost", "predicted", "track_ended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
