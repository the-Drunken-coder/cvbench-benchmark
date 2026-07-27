from __future__ import annotations

import cv2
import numpy as np

from cvbench.examples.reference_mot import (
    PROFILES,
    Detection,
    OnlineTracker,
    YoloXDetector,
)


def test_synthetic_detector_uses_pixels_and_returns_source_box() -> None:
    image = np.full((120, 160, 3), (20, 24, 31), dtype=np.uint8)
    cv2.rectangle(image, (30, 40), (54, 72), (20, 220, 20), -1)

    detections = YoloXDetector._synthetic_detections(image)

    assert len(detections) == 1
    assert detections[0].class_id == "synthetic_target"
    assert detections[0].box == (30.0, 40.0, 55.0, 73.0)


def test_low_confidence_detection_updates_but_does_not_start_lite_track() -> None:
    tracker = OnlineTracker(PROFILES["lite"])
    low = Detection((10.0, 10.0, 30.0, 30.0), "person", 0.12)
    assert tracker.update([low], 100, 100) == []

    high = Detection((10.0, 10.0, 30.0, 30.0), "person", 0.8)
    started = tracker.update([high], 100, 100)
    assert [(event, state, support) for event, _, state, support in started] == [
        ("track_started", "confirmed", "observed")
    ]

    updated = tracker.update([low], 100, 100)
    assert [(event, state, support) for event, _, state, support in updated] == [
        ("track_update", "confirmed", "observed")
    ]


def test_one_hit_track_does_not_generate_predicted_coast() -> None:
    tracker = OnlineTracker(PROFILES["lite"])
    detection = Detection((10.0, 10.0, 30.0, 30.0), "synthetic_target", 0.98)
    tracker.update([detection], 100, 100)

    assert tracker.update([], 100, 100) == []


def test_confirmed_track_coasts_and_reacquires_with_same_id() -> None:
    tracker = OnlineTracker(PROFILES["balanced"])
    first = Detection((10.0, 10.0, 30.0, 30.0), "person", 0.9)
    second = Detection((12.0, 10.0, 32.0, 30.0), "person", 0.9)
    identifier = tracker.update([first], 100, 100)[0][1].identifier
    tracker.update([second], 100, 100)

    coast = tracker.update([], 100, 100)
    assert coast[0][0] == "track_update"
    assert coast[0][1].identifier == identifier
    assert coast[0][2:] == ("coasting", "predicted")

    reacquired = tracker.update(
        [Detection((16.0, 10.0, 36.0, 30.0), "person", 0.8)],
        100,
        100,
    )
    assert reacquired[0][0] == "track_reacquired"
    assert reacquired[0][1].identifier == identifier
    assert reacquired[0][2:] == ("reacquired", "observed")


def test_track_ends_once_immediately_after_profile_coasting_limit() -> None:
    tracker = OnlineTracker(PROFILES["lite"])
    detection = Detection((10.0, 10.0, 30.0, 30.0), "person", 0.9)
    tracker.update([detection], 100, 100)
    tracker.update([detection], 100, 100)

    for _ in range(PROFILES["lite"].coast_frames):
        assert tracker.update([], 100, 100)[0][2:] == ("coasting", "predicted")
    ended = tracker.update([], 100, 100)
    assert [(event, state, support) for event, _, state, support in ended] == [
        ("track_ended", "lost", "predicted")
    ]
    assert tracker.update([], 100, 100) == []


def test_advanced_profile_uses_native_yolox_l_input_and_long_reactivation() -> None:
    profile = PROFILES["advanced"]
    assert profile.input_size == 640
    assert profile.inference_interval == 15
    assert profile.max_misses == 90
    assert profile.observation_centric is True


def test_optical_flow_propagates_track_from_current_pixels() -> None:
    previous = np.zeros((80, 100), dtype=np.uint8)
    current = np.zeros_like(previous)
    cv2.rectangle(previous, (20, 20), (39, 39), 255, -1)
    cv2.rectangle(current, (24, 22), (43, 41), 255, -1)
    track = OnlineTracker(PROFILES["advanced"]).update(
        [Detection((20.0, 20.0, 40.0, 40.0), "person", 0.9)],
        100,
        80,
    )[0][1]

    detections = YoloXDetector._optical_detections(previous, current, [track], 100, 80)

    assert len(detections) == 1
    assert detections[0].class_id == "person"
    assert detections[0].box[0] > 21.0
    assert detections[0].box[1] > 20.5


def test_static_optical_track_is_not_reacquired_and_expires() -> None:
    image = np.zeros((80, 100), dtype=np.uint8)
    tracker = OnlineTracker(PROFILES["advanced"])
    tracker.update([Detection((20.0, 20.0, 40.0, 40.0), "person", 0.9)], 100, 80)
    tracker.update([Detection((20.0, 20.0, 40.0, 40.0), "person", 0.9)], 100, 80)

    for _ in range(PROFILES["advanced"].coast_frames):
        detections = YoloXDetector._optical_detections(
            image,
            image,
            list(tracker.tracks.values()),
            100,
            80,
        )
        assert detections == []
        assert tracker.update(detections, 100, 80)[0][2:] == ("coasting", "predicted")

    assert tracker.update([], 100, 80)[0][0] == "track_ended"
