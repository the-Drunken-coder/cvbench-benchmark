import io
import math

import pytest

from cvbench.errors import ProtocolError
from cvbench.protocol import (
    encode_message,
    receive_message,
    validate_bbox,
    validate_ground_truth,
    validate_track_record,
)
from tests.helpers import gt, output


def test_valid_track_record_is_normalized() -> None:
    record = output(123).system_record
    assert validate_track_record(record)["geometry"]["value"] == [10.0, 10.0, 20.0, 20.0]


@pytest.mark.parametrize("support", ["observed", "predicted"])
def test_required_support_values(support: str) -> None:
    record = output(123, support=support).system_record
    assert validate_track_record(record)["support"] == support


@pytest.mark.parametrize(
    "box",
    [
        [1, 2, 3],
        [2, 2, 1, 3],
        [1, 3, 2, 2],
        [1, 2, math.nan, 4],
        [1, 2, math.inf, 4],
    ],
)
def test_malformed_boxes_are_rejected(box: list[float]) -> None:
    with pytest.raises(ProtocolError):
        validate_bbox(box)


def test_out_of_bounds_policy_is_explicit() -> None:
    with pytest.raises(ProtocolError):
        validate_bbox([-1, 2, 12, 13], width=10, height=10)
    assert validate_bbox([-1, 2, 12, 13], width=10, height=10, out_of_bounds="clip") == [0, 2.0, 10, 10]


def test_timestamp_must_be_non_negative_integer() -> None:
    record = output(123).system_record
    record["source_timestamp_ns"] = -1
    with pytest.raises(ProtocolError):
        validate_track_record(record)


@pytest.mark.parametrize("field", ["sequence_id", "track_id"])
def test_track_identifiers_must_be_non_empty(field: str) -> None:
    record = output(123).system_record
    record[field] = ""
    with pytest.raises(ProtocolError):
        validate_track_record(record)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_json_numbers_are_rejected(value: float) -> None:
    record = output(123).system_record
    record["confidence"] = value
    with pytest.raises(ProtocolError):
        validate_track_record(record)


def test_ground_truth_validation() -> None:
    assert validate_ground_truth(gt(100))["eligible_for_detection"] is True
    ignored = gt(100)
    ignored["ignore"] = True
    assert validate_ground_truth(ignored)["ignore"] is True
    invalid = gt(100)
    invalid["visibility_fraction"] = 1.1
    with pytest.raises(ProtocolError):
        validate_ground_truth(invalid)


def test_unknown_real_visibility_is_explicit_and_paired_with_unknown_occlusion() -> None:
    record = gt(0)
    record["visibility_fraction"] = None
    record["occlusion"] = "unknown"
    assert validate_ground_truth(record)["visibility_fraction"] is None
    record["occlusion"] = "none"
    with pytest.raises(ProtocolError, match="unknown visibility"):
        validate_ground_truth(record)


@pytest.mark.parametrize(
    "record",
    [
        {**gt(100), "ignore_region": True, "ignore": False, "ignore_region_id": "region"},
        {**gt(100), "ignore": True, "ignore_region": True},
        {**gt(100), "ignore": True, "ignore_region": True, "ignore_region_id": ""},
        {**gt(100), "ignore": True, "ignore_region_id": "region"},
    ],
)
def test_ignore_region_conditionals_are_enforced(record: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        validate_ground_truth(record)


def test_ignore_region_is_valid_only_with_ignore_and_identifier() -> None:
    record = {**gt(100), "ignore": True, "ignore_region": True, "ignore_region_id": "region"}
    assert validate_ground_truth(record)["ignore_region_id"] == "region"


def test_reacquisition_event_is_in_the_track_contract() -> None:
    record = output(123).system_record
    record["event"] = "track_reacquired"
    assert validate_track_record(record)["event"] == "track_reacquired"


def test_binary_frame_round_trip_preserves_timestamp_and_payload() -> None:
    metadata = {"event": "frame", "source_timestamp_ns": 5_066_666_667}
    encoded = encode_message(metadata, b"jpeg")
    decoded, payload = receive_message(io.BytesIO(encoded))
    assert decoded == metadata
    assert payload == b"jpeg"
