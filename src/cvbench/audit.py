"""Compact, bounded audit evidence for operator review."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .json_contract import serialized_json_bytes
from .model import CollectedRecord, Match
from .protocol import TRACK_OBSERVATION_EVENTS

MAX_FRAME_SAMPLES = 64
MAX_PREDICTIONS_PER_FRAME = 16
MAX_FALSE_SEGMENTS = 32
MAX_TIMELINE_SAMPLES = 64
MAX_TARGETS_PER_FRAME = 16
MAX_MATCHES_PER_FRAME = 16
MAX_AUDIT_STRING_BYTES = 256
AUDIT_EVIDENCE_MAX_BYTES = 256 * 1024


def _head_tail(values: list[Any], limit: int = MAX_TIMELINE_SAMPLES) -> list[Any]:
    if len(values) <= limit:
        return values
    head = (limit + 1) // 2
    return [*values[:head], *values[-(limit - head) :]]


def _flag(identifier: str, status: str, reason: str, *, count: int = 0, severity: str = "info") -> dict[str, Any]:
    return {
        "id": identifier,
        "status": status,
        "severity": severity,
        "review_aid_only": True,
        "count": count,
        "reason": reason,
    }


def _serialized_bytes(value: Any) -> int:
    return len(serialized_json_bytes(value))


def _bounded_text(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_AUDIT_STRING_BYTES:
        return value
    suffix = "…[truncated]"
    prefix_bytes = max(0, MAX_AUDIT_STRING_BYTES - len(suffix.encode("utf-8")))
    return encoded[:prefix_bytes].decode("utf-8", errors="ignore") + suffix


def _bound_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [_bound_strings(item) for item in value]
    if isinstance(value, dict):
        return {
            _bounded_text(key) if isinstance(key, str) else key: _bound_strings(item)
            for key, item in value.items()
        }
    return value


def _halve_list(value: list[Any]) -> list[Any]:
    return _head_tail(value, max(1, len(value) // 2)) if len(value) > 1 else []


def _enforce_audit_budget(evidence: dict[str, Any]) -> dict[str, Any]:
    """Bound untrusted text and compact optional arrays until JSON fits the callback budget."""

    bounded = _bound_strings(evidence)
    budget = {"max_bytes": AUDIT_EVIDENCE_MAX_BYTES, "truncated": bounded != evidence}
    omitted = {
        "frame_samples": 0,
        "records_in_omitted_frames": {"ground_truth": 0, "predictions": 0, "matches": 0},
        "other_items": 0,
    }
    evidence = bounded
    evidence["serialized_byte_budget"] = budget
    evidence["budget_omitted"] = omitted

    def omit_frames(samples: list[dict[str, Any]]) -> None:
        omitted["frame_samples"] += len(samples)
        for sample in samples:
            for field in ("ground_truth", "predictions", "matches"):
                omitted["records_in_omitted_frames"][field] += len(sample.get(field, [])) + sample.get(
                    f"{field}_omitted", 0
                )

    def reduce_first_available() -> bool:
        frame_samples = evidence.get("frame_samples")
        if isinstance(frame_samples, list) and len(frame_samples) > 1:
            retained = _halve_list(frame_samples)
            head = (len(retained) + 1) // 2
            tail = len(retained) // 2
            omit_frames(frame_samples[head : len(frame_samples) - tail])
            evidence["frame_samples"] = retained
            evidence["sampled_frame_count"] = len(evidence["frame_samples"])
            return True
        if isinstance(frame_samples, list):
            for sample in frame_samples:
                for field in ("ground_truth", "predictions", "matches"):
                    values = sample.get(field)
                    if isinstance(values, list) and len(values) > 1:
                        retained = _halve_list(values)
                        sample[field] = retained
                        sample[f"{field}_omitted"] += len(values) - len(retained)
                        return True
        for path in (
            ("false_track_segments",),
            ("occlusion_and_reacquisition", "reacquisition_events"),
            ("resources_and_isolation", "resources", "over_time"),
        ):
            current: Any = evidence
            for part in path[:-1]:
                current = current.get(part) if isinstance(current, dict) else None
            values = current.get(path[-1]) if isinstance(current, dict) else None
            if isinstance(values, list) and len(values) > 1:
                retained = _halve_list(values)
                current[path[-1]] = retained
                omitted["other_items"] += len(values) - len(retained)
                return True
        return False

    while _serialized_bytes(evidence) > AUDIT_EVIDENCE_MAX_BYTES and reduce_first_available():
        budget["truncated"] = True

    if _serialized_bytes(evidence) > AUDIT_EVIDENCE_MAX_BYTES:
        budget["truncated"] = True
        omit_frames(evidence["frame_samples"])
        evidence["frame_samples"] = []
        for path in (
            ("false_track_segments",),
            ("occlusion_and_reacquisition", "reacquisition_events"),
            ("resources_and_isolation", "resources", "over_time"),
        ):
            current: Any = evidence
            for part in path[:-1]:
                current = current.get(part) if isinstance(current, dict) else None
            values = current.get(path[-1]) if isinstance(current, dict) else None
            if isinstance(values, list):
                omitted["other_items"] += len(values)
        evidence["false_track_segments"] = []
        evidence["resources_and_isolation"] = {"truncated": True}
        evidence["occlusion_and_reacquisition"] = {"truncated": True}
        evidence["reproducibility"] = {"truncated": True}
        evidence["timeline"] = {"truncated": True}
        evidence["sampled_frame_count"] = 0

    # The fallback above leaves only bounded scalar fields and review flags. Keep
    # this assertion as a development invariant; it is never exposed to a model.
    if _serialized_bytes(evidence) > AUDIT_EVIDENCE_MAX_BYTES:
        return {
            "schema_version": "cvbench.audit/v1",
            "review_disposition": "review_aid_only; never an automatic disqualification",
            "frame_samples": [],
            "sampled_frame_count": 0,
            "source_frame_count": evidence.get("source_frame_count", 0),
            "false_track_segments": [],
            "serialized_byte_budget": {"max_bytes": AUDIT_EVIDENCE_MAX_BYTES, "truncated": True},
            "budget_omitted": omitted,
        }
    return evidence


def build_audit_evidence(
    ground_truth: list[dict[str, Any]],
    collected: list[CollectedRecord],
    matches: list[Match],
    metrics: dict[str, Any],
    feed: dict[str, Any],
    resources: dict[str, Any],
    runtime_isolation: dict[str, Any],
    neutral_outputs: list[dict[str, Any]] | None = None,
    timing_compute: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a small bounded evidence packet; raw JSONL is not retained by the control plane."""

    scoreable_ground_truth = [row for row in ground_truth if not row.get("ignore", False)]
    ignored_ground_truth_count = len(ground_truth) - len(scoreable_ground_truth)
    neutral_by_identity = {
        record["_neutral_output_identity"]: record
        for record in (neutral_outputs or [])
        if record.get("neutral_ignored") and record.get("_neutral_output_identity") is not None
    }
    neutral_annotation_ids = sorted(
        {
            annotation_id
            for record in neutral_by_identity.values()
            for annotation_id in record.get("ignore_annotation_ids", [])
        }
    )
    gt_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in scoreable_ground_truth:
        gt_by_frame[(row["sequence_id"], row["source_timestamp_ns"])].append(row)
    output_by_frame: dict[tuple[str, int], list[CollectedRecord]] = defaultdict(list)
    for item in collected:
        output_by_frame[(item.system_record["sequence_id"], item.system_record["source_timestamp_ns"])].append(item)
    matches_by_frame: dict[tuple[str, int], list[Match]] = defaultdict(list)
    matched_records: set[int] = set()
    for match in matches:
        matches_by_frame[(match.sequence_id, match.source_timestamp_ns)].append(match)
        matched_records.add(id(match.output))
    match_by_target_frame = {
        (match.sequence_id, match.source_timestamp_ns, match.target_id): match for match in matches
    }

    frame_keys = sorted(gt_by_frame)
    sample_count = min(MAX_FRAME_SAMPLES, len(frame_keys))
    selected = (
        [frame_keys[round(index * (len(frame_keys) - 1) / (sample_count - 1))] for index in range(sample_count)]
        if sample_count > 1
        else frame_keys[:1]
    )

    frame_samples = []
    for frame_number, key in enumerate(selected):
        sequence_id, timestamp = key
        predictions = []
        for item in output_by_frame.get(key, [])[:MAX_PREDICTIONS_PER_FRAME]:
            record = item.system_record
            prediction = {
                "track_id": record.get("track_id"),
                "event": record.get("event"),
                "state": record.get("state"),
                "support": record.get("support"),
                "confidence": record.get("confidence"),
                "geometry": record.get("geometry", {}).get("value"),
                "collector_received_timestamp_ns": item.collector_received_timestamp_ns,
                "external_latency_ms": (item.collector_received_timestamp_ns - timestamp) / 1_000_000,
            }
            neutral_record = neutral_by_identity.get(id(record))
            if neutral_record is not None:
                prediction["neutral_ignored"] = True
                prediction["ignore_annotation_ids"] = _head_tail(
                    neutral_record.get("ignore_annotation_ids", []), MAX_TARGETS_PER_FRAME
                )
            predictions.append(prediction)
        predictions_omitted = max(0, len(output_by_frame.get(key, [])) - len(predictions))
        frame_matches = matches_by_frame.get(key, [])
        ground_truth_explanations = []
        for row in gt_by_frame[key][:MAX_TARGETS_PER_FRAME]:
            match = match_by_target_frame.get((sequence_id, timestamp, row["target_id"]))
            observed_match = match is not None and match.output.get("support") == "observed"
            eligible = row["on_screen"] and row["eligible_for_detection"]
            counted_toward_score = {
                "observed_coverage": eligible and observed_match,
                "continuity_coverage": eligible and match is not None,
                "localization": observed_match,
                "acquisition": eligible
                and observed_match
                and match.output.get("state") in {"confirmed", "reacquired"},
            }
            if not row["on_screen"]:
                reason = "off_screen"
            elif not row["eligible_for_detection"] and match is not None:
                reason = "matched_but_ineligible_for_detection"
            elif not row["eligible_for_detection"]:
                reason = "not_eligible"
            elif match is None:
                reason = "eligible_without_gated_match"
            elif observed_match:
                reason = "matched_observed_and_counted"
            else:
                reason = "matched_predicted_for_continuity_only"
            ground_truth_explanations.append(
                {
                    "target_id": row["target_id"],
                    "bbox_xyxy": row.get("bbox_xyxy"),
                    "on_screen": row["on_screen"],
                    "eligible_for_detection": row["eligible_for_detection"],
                    "visibility_fraction": row["visibility_fraction"],
                    "occlusion": row["occlusion"],
                    "denominator_eligible": {
                        "observed_coverage": eligible,
                        "continuity_coverage": eligible,
                        "acquisition": eligible,
                    },
                    "matched": match is not None,
                    "counted_toward_score": counted_toward_score,
                    "counted_as": match.output.get("support") if match else None,
                    "count_reason": reason,
                }
            )
        frame_samples.append(
            {
                "sample_index": frame_number,
                "sequence_id": sequence_id,
                "source_timestamp_ns": timestamp,
                "ground_truth": ground_truth_explanations,
                "ground_truth_omitted": max(0, len(gt_by_frame[key]) - len(ground_truth_explanations)),
                "predictions": predictions,
                "predictions_omitted": predictions_omitted,
                "matches": [
                    {
                        "target_id": match.target_id,
                        "track_id": match.track_id,
                        "iou": match.iou,
                        "center_error_px": match.center_error_px,
                        "support": match.output.get("support"),
                        "state": match.output.get("state"),
                    }
                    for match in frame_matches[:MAX_MATCHES_PER_FRAME]
                ],
                "matches_omitted": max(0, len(frame_matches) - MAX_MATCHES_PER_FRAME),
            }
        )

    false_segments = []
    by_track: dict[tuple[str, str], list[CollectedRecord]] = defaultdict(list)
    neutral_timestamps_by_track: dict[tuple[str, str], set[int]] = defaultdict(set)
    for item in collected:
        record = item.system_record
        if id(record) in neutral_by_identity and record.get("track_id"):
            neutral_timestamps_by_track[(record["sequence_id"], str(record["track_id"]))].add(
                record["source_timestamp_ns"]
            )
        if (
            record.get("event") in TRACK_OBSERVATION_EVENTS
            and record.get("support") == "observed"
            and record.get("track_id")
            and id(record) not in matched_records
            and id(record) not in neutral_by_identity
        ):
            by_track[(record["sequence_id"], str(record["track_id"]))].append(item)
    false_track_segment_count = len(by_track)
    for (sequence_id, track_id), records in sorted(by_track.items()):
        records.sort(key=lambda item: item.system_record["source_timestamp_ns"])
        neutral_timestamps = sorted(
            timestamp
            for timestamp in neutral_timestamps_by_track[(sequence_id, track_id)]
            if records[0].system_record["source_timestamp_ns"]
            <= timestamp
            <= records[-1].system_record["source_timestamp_ns"]
        )
        false_segments.append(
            {
                "sequence_id": sequence_id,
                "track_id": track_id,
                "start_timestamp_ns": records[0].system_record["source_timestamp_ns"],
                "end_timestamp_ns": records[-1].system_record["source_timestamp_ns"],
                "record_count": len(records),
                "supports": sorted({item.system_record.get("support") for item in records}),
                "neutral_ignored_timestamps_ns": _head_tail(neutral_timestamps, MAX_TARGETS_PER_FRAME),
                "neutral_ignored_timestamp_count": len(neutral_timestamps),
            }
        )
        if len(false_segments) == MAX_FALSE_SEGMENTS:
            break

    timing_violations = [
        item
        for item in collected
        if item.collector_received_timestamp_ns < item.system_record["source_timestamp_ns"]
    ]
    exact_replays = 0
    comparable_predictions = 0
    for match in matches:
        prediction = match.output.get("geometry", {}).get("value")
        truth = match.gt.get("bbox_xyxy")
        if isinstance(prediction, list) and isinstance(truth, list):
            comparable_predictions += 1
            exact_replays += int(prediction == truth)
    exact_rate = exact_replays / comparable_predictions if comparable_predictions else None
    perfect_metrics = (
        metrics.get("coverage", {}).get("overall_observed") == 1
        and metrics.get("localization", {}).get("mean_iou") == 1
        and metrics.get("identity", {}).get("id_switches", 0) == 0
        and metrics.get("false_detections", {}).get("track_births", 0) == 0
    )
    timing_flagged = bool(timing_violations)
    exact_replay_flagged = exact_rate is not None and comparable_predictions >= 10 and exact_rate >= 0.98
    perfect_flagged = perfect_metrics and metrics.get("sample_counts", {}).get("matches", 0) >= 10
    unread_input_flagged = bool(feed.get("delivered_frames", 0) and not collected)
    network_isolation_ok = runtime_isolation.get("network_mode") == "none"
    network_isolation_observed = network_isolation_ok or runtime_isolation.get("status") == "verified"
    resource_evidence_ok = resources.get("sample_count", 0) > 0 and bool(runtime_isolation)
    flags = [
        _flag(
            "future_frame_lookahead",
            "flagged" if timing_flagged else "clear",
            "Some output was timestamped before its corresponding frame timestamp."
            if timing_flagged
            else "No output preceded its source timestamp.",
            count=len(timing_violations),
            severity="high" if timing_flagged else "info",
        ),
        _flag(
            "output_before_delivery",
            "flagged" if timing_flagged else "clear",
            "External collector timing found an impossible negative latency."
            if timing_flagged
            else "External collector timing is non-negative.",
            count=len(timing_violations),
            severity="high" if timing_flagged else "info",
        ),
        _flag(
            "exact_ground_truth_replay",
            "flagged" if exact_replay_flagged else "clear",
            f"Exact geometry rate is {exact_rate:.3f}."
            if exact_rate is not None
            else "No comparable matched geometries.",
            count=exact_replays,
            severity="medium",
        ),
        _flag(
            "impossible_latency",
            "flagged" if timing_flagged else "clear",
            "The external clock observed a negative delivery latency."
            if timing_flagged
            else "No negative delivery latency was observed.",
            count=len(timing_violations),
            severity="high" if timing_flagged else "info",
        ),
        _flag(
            "anomalously_perfect",
            "flagged" if perfect_flagged else "clear",
            "All primary score components are perfect; operator review is warranted."
            if perfect_metrics
            else "Primary score components are not uniformly perfect.",
            severity="medium",
        ),
        _flag(
            "unread_input",
            "flagged" if unread_input_flagged else "clear",
            "Frames were delivered but no valid submitted-system output was collected."
            if unread_input_flagged
            else "The run produced output or no frames were delivered.",
            severity="medium",
        ),
        _flag(
            "annotation_source_path_access",
            "not_observed",
            "The runner does not expose annotation paths to the submitted system under test (SUT).",
        ),
        _flag(
            "network_isolation",
            "clear" if network_isolation_ok else "flagged" if network_isolation_observed else "not_observed",
            "Runtime isolation reports network none."
            if network_isolation_ok
            else "Runtime isolation was observed without network none."
            if network_isolation_observed
            else "Local execution does not prove the Linux container network boundary.",
            severity="info" if network_isolation_ok else "high" if network_isolation_observed else "info",
        ),
        _flag(
            "resource_isolation",
            "clear" if resource_evidence_ok else "not_observed",
            "Runner resource and isolation evidence is present."
            if resource_evidence_ok
            else "Resource sampling was unavailable for this run.",
        ),
    ]
    occlusion_events = [
        row
        for row in scoreable_ground_truth
        if row.get("occlusion") in {"partial", "full"} or row.get("reappearance_event")
    ]
    eligible_target_keys = {
        (row["sequence_id"], row["target_id"])
        for row in scoreable_ground_truth
        if row["on_screen"] and row["eligible_for_detection"]
    }
    score_explanation = {
        "ground_truth_records": len(scoreable_ground_truth),
        "ignored_ground_truth_records": ignored_ground_truth_count,
        "scoreable_target_denominator": len(eligible_target_keys),
        "matched_continuity": len(matches),
        "matched_observed": sum(match.output.get("support") == "observed" for match in matches),
        "excluded_off_screen": sum(not row["on_screen"] for row in scoreable_ground_truth),
        "excluded_not_eligible": sum(
            row["on_screen"] and not row["eligible_for_detection"] for row in scoreable_ground_truth
        ),
        "ineligible_rows_with_matches": sum(
            row["on_screen"]
            and not row["eligible_for_detection"]
            and (row["sequence_id"], row["source_timestamp_ns"], row["target_id"]) in match_by_target_frame
            for row in scoreable_ground_truth
        ),
        "eligible_without_gated_match": sum(
            row["on_screen"]
            and row["eligible_for_detection"]
            and (row["sequence_id"], row["source_timestamp_ns"], row["target_id"]) not in match_by_target_frame
            for row in scoreable_ground_truth
        ),
        "component_eligibility": {
            "observed_coverage": "on_screen and eligible_for_detection with an observed gated match",
            "continuity_coverage": "on_screen and eligible_for_detection with any gated match",
            "localization": "any observed gated match, including an ineligible row when geometry is scored",
            "acquisition": "on_screen and eligible_for_detection with an observed confirmed or reacquired match",
        },
        "coverage_denominators": {
            "eligible_rows": sum(
                row["on_screen"] and row["eligible_for_detection"] for row in scoreable_ground_truth
            ),
            "eligible_targets": len(eligible_target_keys),
            "observed_coverage": sum(
                row["on_screen"] and row["eligible_for_detection"] for row in scoreable_ground_truth
            ),
            "continuity_coverage": sum(
                row["on_screen"] and row["eligible_for_detection"] for row in scoreable_ground_truth
            ),
        },
        "positive_credit_meaning": (
            "component_counts records positive credit, while coverage_denominators records eligible rows "
            "including misses"
        ),
        "component_counts": {
            "observed_coverage": sum(
                row["on_screen"]
                and row["eligible_for_detection"]
                and (
                    match_by_target_frame.get(
                        (row["sequence_id"], row["source_timestamp_ns"], row["target_id"])
                    )
                    is not None
                )
                and match_by_target_frame[
                    (row["sequence_id"], row["source_timestamp_ns"], row["target_id"])
                ].output.get("support")
                == "observed"
                for row in scoreable_ground_truth
            ),
            "continuity_coverage": sum(
                row["on_screen"]
                and row["eligible_for_detection"]
                and (row["sequence_id"], row["source_timestamp_ns"], row["target_id"]) in match_by_target_frame
                for row in scoreable_ground_truth
            ),
            "localization": sum(
                match.output.get("support") == "observed" for match in matches
            ),
            "acquisition": sum(
                match.output.get("support") == "observed"
                and match.output.get("state") in {"confirmed", "reacquired"}
                and match.gt["on_screen"]
                and match.gt["eligible_for_detection"]
                for match in matches
            ),
        },
    }
    return _enforce_audit_budget({
        "schema_version": "cvbench.audit/v1",
        "review_disposition": "review_aid_only; never an automatic disqualification",
        "frame_samples": frame_samples,
        "sampled_frame_count": len(frame_samples),
        "source_frame_count": len(frame_keys),
        "false_track_segments": false_segments,
        "false_track_segment_count": false_track_segment_count,
        "neutral_ignored_predictions": {
            "count": len(neutral_by_identity),
            "annotation_ids": _head_tail(neutral_annotation_ids, MAX_TARGETS_PER_FRAME),
            "annotation_ids_omitted": max(0, len(neutral_annotation_ids) - MAX_TARGETS_PER_FRAME),
        },
        "score_explanation": score_explanation,
        "occlusion_and_reacquisition": {
            "occlusion_rows": len(occlusion_events),
            "reacquisition_events": _head_tail(metrics.get("reacquisition", {}).get("by_gap", [])),
            "feed_fault_recovery": {
                kind: {**data, "details": _head_tail(data.get("details", []))}
                for kind, data in metrics.get("robustness", {}).get("feed_faults", {}).items()
            },
        },
        "timeline": {
            "external_clock": "collector_received_timestamp_ns",
            "support_counts": {
                "observed": sum(item.system_record.get("support") == "observed" for item in collected),
                "predicted": sum(item.system_record.get("support") == "predicted" for item in collected),
            },
            "state_counts": {
                state: sum(item.system_record.get("state") == state for item in collected)
                for state in ("tentative", "confirmed", "coasting", "reacquired", "lost")
            },
        },
        "resources_and_isolation": {
            "resources": {**resources, "over_time": _head_tail(resources.get("over_time", []))},
            "runtime_isolation": runtime_isolation,
        },
        "timing_compute": timing_compute,
        "reproducibility": {"feed_counters": feed, "raw_evidence_available": False},
        "flags": flags,
    })
