from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
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
    center: tuple[float, float]
    velocity: tuple[float, float] = (0.0, 0.0)
    hits: int = 1
    misses: int = 0
    was_missing: bool = False
    ended: bool = False


def _detections(payload: bytes) -> list[list[float]]:
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([35, 80, 50]), np.array([90, 255, 255]))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width * height >= 40:
            boxes.append([float(x), float(y), float(x + width), float(y + height)])
    return sorted(boxes, key=lambda box: (box[0], box[1]))


def _center(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _clamp_box(box: list[float], width: int, height: int) -> list[float]:
    left = min(max(0.0, box[0]), float(width - 1))
    top = min(max(0.0, box[1]), float(height - 1))
    right = min(max(left + 1.0, box[2]), float(width))
    bottom = min(max(top + 1.0, box[3]), float(height))
    return [left, top, right, bottom]


def _emit(event: str, metadata: dict[str, Any], track: Track, state: str, support: str) -> None:
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
                "class_id": "synthetic_target",
                "confidence": 0.92 if support == "observed" else max(0.2, 0.8 - track.misses * 0.12),
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
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _pacing_mode() -> tuple[str, float]:
    mode = os.environ.get("CVBENCH_PACING_EVIDENCE_MODE", "fast")
    if mode not in {
        "fast",
        "cpu-heavy",
        "idle",
        "background-child-cpu",
        "final-burst",
        "immediate-exit",
    }:
        raise ValueError("CVBENCH_PACING_EVIDENCE_MODE is invalid")
    try:
        delay = float(os.environ.get("CVBENCH_PACING_EVIDENCE_DELAY_SECONDS", "0.15"))
    except ValueError as exc:
        raise ValueError("CVBENCH_PACING_EVIDENCE_DELAY_SECONDS is invalid") from exc
    if not 0 <= delay <= 1:
        raise ValueError("CVBENCH_PACING_EVIDENCE_DELAY_SECONDS must be between 0 and 1")
    return mode, delay


def _pace_frame(mode: str, delay: float) -> None:
    if mode == "idle":
        time.sleep(delay)
    elif mode == "cpu-heavy":
        deadline = time.perf_counter() + delay
        value = 1
        while time.perf_counter() < deadline:
            value = (value * 1_664_525 + 1_013_904_223) & 0xFFFFFFFF
        if value < 0:  # pragma: no cover - keeps the loop result observable
            raise AssertionError


def _background_cpu_worker(mode: str) -> subprocess.Popen[bytes] | None:
    if mode != "background-child-cpu":
        return None
    return subprocess.Popen(
        [sys.executable, "-c", "value=1\nwhile True: value=(value*1664525+1013904223)&0xffffffff"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _final_accounting_burst(mode: str) -> None:
    if mode != "final-burst":
        return
    time.sleep(0.12)
    deadline = time.perf_counter() + 0.15
    value = 1
    while time.perf_counter() < deadline:
        value = (value * 1_664_525 + 1_013_904_223) & 0xFFFFFFFF
    with open("/tmp/cvbench-final-burst.bin", "wb") as handle:
        for _ in range(64):
            handle.write(bytes(64 * 1024))
        handle.flush()
        os.fsync(handle.fileno())
    if value < 0:  # pragma: no cover - keeps the loop result observable
        raise AssertionError


def _stop_background_worker(worker: subprocess.Popen[bytes]) -> None:
    worker.terminate()
    try:
        worker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=2)


def main() -> int:
    path = os.environ.get("CVBENCH_INPUT_SOCKET", "/run/cvbench/input.sock")
    sock = _connect(path)
    print("CVBENCH_READY", flush=True)
    pacing_mode, pacing_delay = _pacing_mode()
    background_worker = _background_cpu_worker(pacing_mode)
    tracks: dict[str, Track] = {}
    next_identifier = 1
    benchmark_ended = False
    try:
        with sock, sock.makefile("rb") as stream:
            while True:
                try:
                    metadata, payload = receive_message(stream)
                except EOFError:
                    break
                event = metadata.get("event")
                if event == "benchmark_end":
                    _final_accounting_burst(pacing_mode)
                    sock.shutdown(socket.SHUT_WR)
                    benchmark_ended = True
                    if pacing_mode == "immediate-exit":
                        break
                    continue
                if benchmark_ended:
                    continue
                if event == "stream_start":
                    tracks.clear()
                    continue
                if event != "frame":
                    continue
                _pace_frame(pacing_mode, pacing_delay)
                detections = _detections(payload)
                available = set(tracks)
                assignments: list[tuple[str, list[float]]] = []
                for box in detections:
                    center = _center(box)
                    candidates = sorted(
                        (
                            distance,
                            identifier,
                        )
                        for identifier, track in tracks.items()
                        if identifier in available
                        and (
                            distance := (track.center[0] + track.velocity[0] - center[0]) ** 2
                            + (track.center[1] + track.velocity[1] - center[1]) ** 2
                        )
                        <= (12 if track.ended else 45) ** 2
                    )
                    if candidates:
                        identifier = candidates[0][1]
                        available.remove(identifier)
                    else:
                        identifier = f"classical-{next_identifier}"
                        next_identifier += 1
                        tracks[identifier] = Track(identifier, box, center)
                    assignments.append((identifier, box))
                matched = {identifier for identifier, _ in assignments}
                for identifier, box in assignments:
                    track = tracks[identifier]
                    created = track.hits == 1 and track.misses == 0 and not track.was_missing
                    was_missing = track.was_missing
                    new_center = _center(box)
                    track.velocity = (new_center[0] - track.center[0], new_center[1] - track.center[1])
                    track.center = new_center
                    track.box = box
                    track.hits += 1
                    state = "reacquired" if was_missing else ("confirmed" if track.hits >= 2 else "tentative")
                    event_name = "track_started" if created else ("track_reacquired" if was_missing else "track_update")
                    track.misses = 0
                    track.was_missing = False
                    track.ended = False
                    _emit(event_name, metadata, track, state, "observed")
                for identifier in list(tracks):
                    if identifier in matched:
                        continue
                    track = tracks[identifier]
                    track.misses += 1
                    track.was_missing = True
                    track.box = _clamp_box(
                        [
                            track.box[0] + track.velocity[0],
                            track.box[1] + track.velocity[1],
                            track.box[2] + track.velocity[0],
                            track.box[3] + track.velocity[1],
                        ],
                        int(metadata["width"]),
                        int(metadata["height"]),
                    )
                    track.center = _center(track.box)
                    if track.misses <= 5:
                        _emit("track_update", metadata, track, "coasting", "predicted")
                    elif not track.ended:
                        _emit("track_ended", metadata, track, "lost", "predicted")
                        track.ended = True
    finally:
        if background_worker is not None:
            _stop_background_worker(background_worker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
