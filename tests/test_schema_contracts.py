import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from tests.helpers import gt, output

ROOT = Path(__file__).parents[1]


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schemas" / name).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_frame_event_requires_complete_frame_metadata() -> None:
    validator = _validator("frame-v1.schema.json")
    frame = {
        "schema_version": "cvbench.frame/v1",
        "event": "frame",
        "sequence_id": "sequence",
        "frame_index": 0,
        "source_timestamp_ns": 0,
        "native_source_timestamp_ns": 0,
        "width": 160,
        "height": 120,
        "pixel_format": "rgb24",
        "payload_encoding": "jpeg",
    }
    validator.validate(frame)
    required_frame_fields = (
        "frame_index",
        "source_timestamp_ns",
        "native_source_timestamp_ns",
        "width",
        "height",
        "pixel_format",
        "payload_encoding",
    )
    for field in required_frame_fields:
        incomplete = dict(frame)
        incomplete.pop(field)
        with pytest.raises(ValidationError):
            validator.validate(incomplete)
    validator.validate({"schema_version": "cvbench.frame/v1", "event": "benchmark_end"})
    with pytest.raises(ValidationError):
        validator.validate({
            "schema_version": "cvbench.frame/v1",
            "event": "benchmark_end",
            "frame_index": 0,
        })
    with pytest.raises(ValidationError):
        validator.validate({
            **frame,
            "timing_compute_contract": "cvbench.timing-compute/v1",
        })
    with pytest.raises(ValidationError):
        validator.validate({**frame, "unexpected": True})
    with pytest.raises(ValidationError):
        validator.validate({"schema_version": "cvbench.frame/v1", "event": "stream_start", "sequence_id": "sequence"})


def test_ground_truth_bbox_requirement_matches_runtime_contract() -> None:
    validator = _validator("ground-truth-v1.schema.json")
    visible = gt(0)
    validator.validate(visible)
    visible.pop("bbox_xyxy")
    with pytest.raises(ValidationError):
        validator.validate(visible)
    off_screen = {**visible, "on_screen": False}
    validator.validate(off_screen)


def test_ground_truth_ignore_region_conditionals_are_complete() -> None:
    validator = _validator("ground-truth-v1.schema.json")
    valid = {**gt(0), "ignore": True, "ignore_region": True, "ignore_region_id": "region"}
    validator.validate(valid)
    for invalid in (
        {**gt(0), "ignore_region": True, "ignore": False, "ignore_region_id": "region"},
        {**gt(0), "ignore": True, "ignore_region": True},
        {**gt(0), "ignore": True, "ignore_region": True, "ignore_region_id": ""},
        {**gt(0), "ignore": True, "ignore_region_id": "region"},
    ):
        with pytest.raises(ValidationError):
            validator.validate(invalid)


@pytest.mark.parametrize("field", ["sequence_id", "track_id"])
def test_track_schema_rejects_empty_identifiers(field: str) -> None:
    validator = _validator("track-v1.schema.json")
    record = output(0).system_record
    record[field] = ""
    with pytest.raises(ValidationError):
        validator.validate(record)


def test_timing_compute_schema_requires_immutable_source_and_allowlisted_replay() -> None:
    validator = _validator("timing-compute-v1.schema.json")
    timing = {
        "contract_version": "cvbench.timing-compute/v1",
        "source": {
            "immutable": True,
            "frame_count": 2,
            "duration_seconds": 1,
            "sequences": [],
        },
        "replay": {
            "profile": "half-speed",
            "rate": 0.5,
            "native_real_time": False,
            "allowlisted": True,
        },
        "durations": {
            "wall_seconds": 2,
            "runner_total_seconds": 2.1,
            "teardown_seconds": 0.1,
            "startup_seconds": 0.1,
            "stream_delivery_seconds": 2,
            "completion_seconds": 2,
            "drain_seconds": 0,
            "real_time_factor": 2,
        },
        "delivery": {
            "policy_version": "cvbench.delivery-lossless/v1",
            "replay_profile": "half-speed",
            "replay_rate": 0.5,
            "effective_replay_rate": 0.5,
            "delivered_frames_per_second": 20,
            "deadline_missed_frames": 0,
            "sender_pressure_frames": 0,
            "frame_count": 0,
            "delivered_frames": 0,
            "transport_failed_frames": 0,
            "policy_dropped_frames": 0,
            "sender_blocking_time_ms": 0,
            "benchmark_end_sender_call_ms": 0,
            "delivery_backlog_ms": {"sample_count": 0, "minimum": None, "median": None, "p95": None, "maximum": None},
            "sender_call_ms": {"sample_count": 0, "minimum": None, "median": None, "p95": None, "maximum": None},
            "input_queue_depth": None,
            "input_queue_depth_available": False,
            "input_queue_depth_note": "not portable",
            "semantics": "ordered",
            "per_frame": [],
        },
        "processing_latency_ms": {"sample_count": 0, "minimum": None, "median": None, "p95": None, "maximum": None},
        "native_source_offset_ms": {"sample_count": 0, "minimum": None, "median": None, "p95": None, "maximum": None},
        "output": {
            "records": 0,
            "records_per_native_source_second": 0,
            "records_per_completion_second": 0,
            "late_after_benchmark_end": 0,
            "late_output_policy": "bounded",
        },
        "clocks": {"source": "source", "delivery": "delivery", "completion": "completion"},
    }
    validator.validate(timing)
    timing["replay"]["rate"] = 0.3
    with pytest.raises(ValidationError):
        validator.validate(timing)


def test_report_schema_has_no_unconstrained_object_contracts() -> None:
    schema = json.loads((ROOT / "schemas" / "report-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert "timing" in schema["required"]
    assert "resources" in schema["required"]

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert "additionalProperties" in value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


def test_redacted_report_schema_is_distinct_and_recursively_strict() -> None:
    schema = json.loads((ROOT / "schemas" / "report-redacted-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"] == "cvbench.report-redacted/v1"
    assert schema["properties"]["source_schema_version"]["const"] == "cvbench.report/v1"

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert "additionalProperties" in value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
