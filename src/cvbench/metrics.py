from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from .config import Thresholds
from .matching import _hungarian, bbox_iou, center_error, match_records_by_support
from .model import CollectedRecord, Match
from .protocol import TRACK_EVENTS, TRACK_OBSERVATION_EVENTS


def _track_id_lifecycle(
    outputs: list[dict[str, Any]], observed_matches: list[Match]
) -> dict[str, Any]:
    assignments = {
        id(match.output): match
        for match in observed_matches
        if match.output.get("event") in TRACK_OBSERVATION_EVENTS
        and match.output.get("state") != "lost"
    }
    events: list[dict[str, Any]] = []
    for record in outputs:
        track_id = record.get("track_id")
        if not track_id:
            continue
        terminal = record.get("event") == "track_ended" or record.get("state") == "lost"
        if terminal:
            events.append(
                {
                    "kind": "terminal",
                    "sequence_id": record["sequence_id"],
                    "source_timestamp_ns": record["source_timestamp_ns"],
                    "track_id": str(track_id),
                    "target_id": None,
                }
            )
        match = assignments.get(id(record))
        if match is not None:
            events.append(
                {
                    "kind": "assignment",
                    "sequence_id": match.sequence_id,
                    "source_timestamp_ns": match.source_timestamp_ns,
                    "track_id": match.track_id,
                    "target_id": match.target_id,
                }
            )
    events.sort(
        key=lambda event: (
            event["sequence_id"],
            event["source_timestamp_ns"],
            0 if event["kind"] == "terminal" else 1,
            event["track_id"],
            event["target_id"] or "",
        )
    )
    assigned_target: dict[tuple[str, str], str] = {}
    active: dict[tuple[str, str], bool] = {}
    last_assignment_timestamp: dict[tuple[str, str], int] = {}
    physical_births: set[tuple[str, str]] = set()
    evidence: list[dict[str, Any]] = []
    for event in events:
        key = (event["sequence_id"], event["track_id"])
        if event["kind"] == "terminal":
            active[key] = False
            continue
        target_id = event["target_id"]
        assert isinstance(target_id, str)
        physical_births.add((event["sequence_id"], target_id))
        previous_target = assigned_target.get(key)
        if previous_target is not None and previous_target != target_id:
            reuse_kind = "active_target_alias" if active.get(key, False) else "reuse_after_terminal"
            evidence.append(
                {
                    "kind": reuse_kind,
                    "sequence_id": event["sequence_id"],
                    "track_id": event["track_id"],
                    "previous_target_id": previous_target,
                    "new_target_id": target_id,
                    "previous_assignment_timestamp_ns": last_assignment_timestamp[key],
                    "reuse_timestamp_ns": event["source_timestamp_ns"],
                }
            )
        if previous_target != target_id:
            assigned_target[key] = target_id
        last_assignment_timestamp[key] = event["source_timestamp_ns"]
        active[key] = True
    ended_reuse = sum(event["kind"] == "reuse_after_terminal" for event in evidence)
    active_alias = sum(event["kind"] == "active_target_alias" for event in evidence)
    return {
        "unique_track_ids": len(assigned_target),
        "distinct_physical_target_births": len(physical_births),
        "track_id_reuse_detected": bool(evidence),
        "track_id_reuse_events": len(evidence),
        "ended_track_id_reuse_events": ended_reuse,
        "active_track_id_alias_events": active_alias,
        "track_id_reuse_evidence": evidence,
    }


def percentile(values: Iterable[float], p: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "sample_count": len(values),
        "median": percentile(values, 0.5),
        "p90": percentile(values, 0.9),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "maximum": max(values) if values else None,
    }


def _frame_durations(ground_truth: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    timestamps: dict[str, list[int]] = defaultdict(list)
    for gt in ground_truth:
        timestamps[gt["sequence_id"]].append(gt["source_timestamp_ns"])
    result: dict[tuple[str, int], int] = {}
    for sequence, raw_timestamps in timestamps.items():
        unique = sorted(set(raw_timestamps))
        steps = [right - left for left, right in zip(unique, unique[1:], strict=False) if right > left]
        fallback = int(statistics.median(steps)) if steps else 0
        for index, timestamp in enumerate(unique):
            result[(sequence, timestamp)] = unique[index + 1] - timestamp if index + 1 < len(unique) else fallback
    return result


def _dropout_intervals(rows: list[tuple[int, int, bool]], tolerance_ns: int) -> list[int]:
    intervals: list[int] = []
    active = 0
    for _, duration, observed in rows:
        if observed:
            if active > tolerance_ns:
                intervals.append(active)
            active = 0
        else:
            active += duration
    if active > tolerance_ns:
        intervals.append(active)
    return intervals


def _mot_detections(
    ground_truth: list[dict[str, Any]], outputs: list[dict[str, Any]]
) -> tuple[dict[tuple[str, int], list[dict[str, Any]]], dict[tuple[str, int], list[dict[str, Any]]]]:
    truth_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    output_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in ground_truth:
        if not row.get("ignore") and row.get("on_screen") and row.get("eligible_for_detection"):
            truth_by_frame[(row["sequence_id"], row["source_timestamp_ns"])].append(row)
    for row in outputs:
        if row.get("event") in TRACK_OBSERVATION_EVENTS and row.get("state") != "lost":
            output_by_frame[(row["sequence_id"], row["source_timestamp_ns"])].append(row)
    return truth_by_frame, output_by_frame


def _mot_identity(row: dict[str, Any], *, truth: bool) -> tuple[str, str, str]:
    return (
        str(row["sequence_id"]),
        str(row.get("class_id", "")),
        str(row["target_id"] if truth else row["track_id"]),
    )


def _mot_pair_iou(truth: dict[str, Any], output: dict[str, Any]) -> float:
    if truth.get("class_id") != output.get("class_id"):
        return 0.0
    return bbox_iou(truth["bbox_xyxy"], output["geometry"]["value"])


def _frame_assignment(
    truths: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    scores: list[list[float]],
    similarities: list[list[float]],
    threshold: float,
) -> list[tuple[int, int]]:
    if not truths or not outputs:
        return []
    cost = [
        [
            -(scores[row][column]) if similarities[row][column] >= threshold else 1_000_000.0
            for column in range(len(outputs))
        ]
        for row in range(len(truths))
    ]
    return [
        (row, column)
        for row, column in _hungarian(cost)
        if similarities[row][column] >= threshold and cost[row][column] < 1_000_000.0
    ]


def _calculate_mot_metrics(
    ground_truth: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    scenario_families: dict[str, str] | None = None,
    *,
    include_scenario_breakdown: bool = True,
) -> dict[str, Any]:
    """Calculate class-aware HOTA and IDF1 over complete tracker hypotheses.

    HOTA follows the TrackEval global-alignment formulation at IoU thresholds
    0.05 through 0.95. IDF1 uses a global identity assignment after class-aware
    per-frame IoU >= 0.5 matching. Sequence IDs are part of every identity, so
    identities cannot leak across scenario boundaries.
    """
    truth_by_frame, output_by_frame = _mot_detections(ground_truth, outputs)
    frames = sorted(set(truth_by_frame) | set(output_by_frame))
    truth_counts: Counter[tuple[str, str, str]] = Counter()
    output_counts: Counter[tuple[str, str, str]] = Counter()
    potential: Counter[tuple[tuple[str, str, str], tuple[str, str, str]]] = Counter()
    frame_data: list[tuple[list[dict[str, Any]], list[dict[str, Any]], list[list[float]]]] = []
    for frame in frames:
        truths = truth_by_frame.get(frame, [])
        predictions = output_by_frame.get(frame, [])
        similarities = [[_mot_pair_iou(truth, prediction) for prediction in predictions] for truth in truths]
        for truth in truths:
            truth_counts[_mot_identity(truth, truth=True)] += 1
        for prediction in predictions:
            output_counts[_mot_identity(prediction, truth=False)] += 1
        for truth_index, truth in enumerate(truths):
            row_sum = sum(similarities[truth_index])
            for output_index, prediction in enumerate(predictions):
                similarity = similarities[truth_index][output_index]
                if similarity <= 0:
                    continue
                column_sum = sum(row[output_index] for row in similarities)
                denominator = row_sum + column_sum - similarity
                if denominator > 0:
                    potential[(_mot_identity(truth, truth=True), _mot_identity(prediction, truth=False))] += (
                        similarity / denominator
                    )
        frame_data.append((truths, predictions, similarities))

    alignment = {
        pair: value / (truth_counts[pair[0]] + output_counts[pair[1]] - value)
        for pair, value in potential.items()
    }
    threshold_rows = []
    for threshold_integer in range(5, 100, 5):
        threshold = threshold_integer / 100
        matches: Counter[tuple[tuple[str, str, str], tuple[str, str, str]]] = Counter()
        true_positives = 0
        localization_sum = 0.0
        for truths, predictions, similarities in frame_data:
            scores = [
                [
                    alignment.get((_mot_identity(truth, truth=True), _mot_identity(prediction, truth=False)), 0.0)
                    * similarities[truth_index][output_index]
                    for output_index, prediction in enumerate(predictions)
                ]
                for truth_index, truth in enumerate(truths)
            ]
            for truth_index, output_index in _frame_assignment(
                truths, predictions, scores, similarities, threshold
            ):
                pair = (
                    _mot_identity(truths[truth_index], truth=True),
                    _mot_identity(predictions[output_index], truth=False),
                )
                matches[pair] += 1
                true_positives += 1
                localization_sum += similarities[truth_index][output_index]
        false_negatives = sum(truth_counts.values()) - true_positives
        false_positives = sum(output_counts.values()) - true_positives
        detection_denominator = true_positives + false_negatives + false_positives
        detection_accuracy = true_positives / detection_denominator if detection_denominator else 1.0
        association_sum = 0.0
        for pair, count in matches.items():
            association_accuracy = count / (truth_counts[pair[0]] + output_counts[pair[1]] - count)
            association_sum += count * association_accuracy
        association_accuracy = association_sum / true_positives if true_positives else 0.0
        threshold_rows.append(
            {
                "iou_threshold": threshold,
                "hota": math.sqrt(detection_accuracy * association_accuracy),
                "detection_accuracy": detection_accuracy,
                "association_accuracy": association_accuracy,
                "localization_accuracy": localization_sum / true_positives if true_positives else 0.0,
                "true_positives": true_positives,
                "false_negatives": false_negatives,
                "false_positives": false_positives,
            }
        )

    identity_pair_counts: Counter[tuple[tuple[str, str, str], tuple[str, str, str]]] = Counter()
    for truths, predictions, similarities in frame_data:
        for truth_index, output_index in _frame_assignment(truths, predictions, similarities, similarities, 0.5):
            identity_pair_counts[
                (
                    _mot_identity(truths[truth_index], truth=True),
                    _mot_identity(predictions[output_index], truth=False),
                )
            ] += 1
    truth_ids = sorted(truth_counts)
    output_ids = sorted(output_counts)
    id_assignment = _hungarian(
        [[-identity_pair_counts[(truth_id, output_id)] for output_id in output_ids] for truth_id in truth_ids]
    ) if truth_ids and output_ids else []
    identity_true_positives = sum(
        identity_pair_counts[(truth_ids[row], output_ids[column])] for row, column in id_assignment
    )
    identity_false_negatives = sum(truth_counts.values()) - identity_true_positives
    identity_false_positives = sum(output_counts.values()) - identity_true_positives
    idf1_denominator = 2 * identity_true_positives + identity_false_negatives + identity_false_positives

    result = {
        "schema_version": "cvbench.mot-metrics/v1",
        "class_aware": True,
        "ground_truth_detections": sum(truth_counts.values()),
        "tracker_detections": sum(output_counts.values()),
        "ground_truth_tracks": len(truth_counts),
        "tracker_tracks": len(output_counts),
        "hota": statistics.fmean(row["hota"] for row in threshold_rows) if threshold_rows else 1.0,
        "detection_accuracy": statistics.fmean(row["detection_accuracy"] for row in threshold_rows)
        if threshold_rows
        else 1.0,
        "association_accuracy": statistics.fmean(row["association_accuracy"] for row in threshold_rows)
        if threshold_rows
        else 1.0,
        "localization_accuracy": statistics.fmean(row["localization_accuracy"] for row in threshold_rows)
        if threshold_rows
        else 1.0,
        "hota_by_iou_threshold": threshold_rows,
        "idf1": 2 * identity_true_positives / idf1_denominator if idf1_denominator else 1.0,
        "identity_true_positives": identity_true_positives,
        "identity_false_negatives": identity_false_negatives,
        "identity_false_positives": identity_false_positives,
        "idf1_match_iou": 0.5,
    }
    if include_scenario_breakdown:
        scenario_families = scenario_families or {}
        sequences_by_scenario: dict[str, set[str]] = defaultdict(set)
        for sequence_id, _ in frames:
            sequences_by_scenario[scenario_families.get(sequence_id, sequence_id)].add(sequence_id)
        result["by_scenario"] = {}
        for scenario, sequences in sorted(sequences_by_scenario.items()):
            scenario_result = _calculate_mot_metrics(
                [row for row in ground_truth if row["sequence_id"] in sequences],
                [row for row in outputs if row["sequence_id"] in sequences],
                include_scenario_breakdown=False,
            )
            result["by_scenario"][scenario] = {
                key: scenario_result[key]
                for key in (
                    "association_accuracy",
                    "ground_truth_detections",
                    "ground_truth_tracks",
                    "hota",
                    "idf1",
                    "tracker_detections",
                    "tracker_tracks",
                )
            }
    return result


def calculate_metrics(
    ground_truth: list[dict[str, Any]],
    collected: list[CollectedRecord],
    thresholds: Thresholds,
    *,
    frame_delivery_ns: dict[tuple[str, int], int] | None = None,
    sequence_timestamps: dict[str, list[int]] | None = None,
    scenario_families: dict[str, str] | None = None,
    fault_timestamps: set[tuple[str, int]] | None = None,
    fault_events: dict[tuple[str, int], list[str]] | None = None,
) -> tuple[dict[str, Any], list[Match]]:
    scenario_families = scenario_families or {}
    fault_timestamps = fault_timestamps or set()
    fault_events = fault_events or {}
    outputs = [item.system_record for item in collected]
    observed_matches, matches, unmatched = match_records_by_support(ground_truth, outputs, thresholds)
    neutral_output_ids = {
        record["_neutral_output_identity"]
        for record in unmatched
        if record.get("neutral_ignored")
    }
    scored_unmatched = [
        record
        for record in unmatched
        if not record.get("neutral_ignored")
    ]
    scored_outputs = [
        record
        for record in outputs
        if id(record) not in neutral_output_ids
    ]
    ground_truth_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for gt in ground_truth:
        if gt.get("ignore", False):
            continue
        ground_truth_by_frame[(gt["sequence_id"], gt["source_timestamp_ns"])].append(gt)
    match_by_target_frame = {
        (match.sequence_id, match.source_timestamp_ns, match.target_id): match for match in matches
    }
    observed_match_by_target_frame = {
        (match.sequence_id, match.source_timestamp_ns, match.target_id): match for match in observed_matches
    }
    durations = _frame_durations(ground_truth)
    eligible_by_target: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    all_by_target: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for gt in ground_truth:
        if gt.get("ignore", False):
            continue
        key = (gt["sequence_id"], gt["target_id"])
        all_by_target[key].append(gt)
        if gt["on_screen"] and gt["eligible_for_detection"]:
            eligible_by_target[key].append(gt)

    acquisition_latencies_ms: list[float] = []
    acquisition_per_target: dict[str, float | None] = {}
    acquired_at: dict[tuple[str, str], int] = {}
    acquired: set[tuple[str, str]] = set()
    for key, rows in sorted(eligible_by_target.items()):
        rows.sort(key=lambda row: row["source_timestamp_ns"])
        eligible_at = rows[0]["source_timestamp_ns"]
        correct = [
            match
            for match in matches
            if (match.sequence_id, match.target_id) == key
            and match.source_timestamp_ns >= eligible_at
            and match.output["state"] in {"confirmed", "reacquired"}
            and match.output["support"] == "observed"
        ]
        latency = None
        if correct:
            latency = (min(match.source_timestamp_ns for match in correct) - eligible_at) / 1_000_000
            acquired_at[key] = min(match.source_timestamp_ns for match in correct)
            acquisition_latencies_ms.append(latency)
            acquired.add(key)
        acquisition_per_target[f"{key[0]}:{key[1]}"] = latency
    acquisition = _summary(acquisition_latencies_ms)
    acquisition.update(
        {
            "total_eligible_targets": len(eligible_by_target),
            "acquired_targets": len(acquired),
            "never_acquired_targets": len(eligible_by_target) - len(acquired),
            "rate": len(acquired) / len(eligible_by_target) if eligible_by_target else None,
            "per_target_latency_ms": acquisition_per_target,
            "within_deadline": {
                str(deadline): sum(value <= deadline for value in acquisition_latencies_ms) / len(eligible_by_target)
                if eligible_by_target
                else None
                for deadline in thresholds.acquisition_deadlines_ms
            },
        }
    )

    total_eligible_ns = 0
    observed_ns = 0
    continuity_ns = 0
    coverage_by_target: dict[str, dict[str, float | int]] = {}
    coverage_by_visibility: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    coverage_by_scenario: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    dropout_durations_ns: list[int] = []
    for key, rows in sorted(eligible_by_target.items()):
        rows.sort(key=lambda row: row["source_timestamp_ns"])
        target_total = target_observed = target_continuity = 0
        dropout_rows: list[tuple[int, int, bool]] = []
        for row in rows:
            duration = durations.get((row["sequence_id"], row["source_timestamp_ns"]), 0)
            match = match_by_target_frame.get((row["sequence_id"], row["source_timestamp_ns"], row["target_id"]))
            is_active = bool(match and match.output["event"] != "track_ended" and match.output["state"] != "lost")
            is_observed = bool(is_active and match and match.output["support"] == "observed")
            is_continuous = is_active
            target_total += duration
            target_observed += duration if is_observed else 0
            target_continuity += duration if is_continuous else 0
            visibility_fraction = row.get("visibility_fraction")
            if visibility_fraction is not None:
                visibility = (
                    "full" if visibility_fraction >= 0.8 else ("partial" if visibility_fraction >= 0.3 else "low")
                )
                coverage_by_visibility[visibility][0] += duration if is_observed else 0
                coverage_by_visibility[visibility][1] += duration
            family = str(row.get("scenario_family", "unknown"))
            coverage_by_scenario[family][0] += duration if is_observed else 0
            coverage_by_scenario[family][1] += duration
            if row["source_timestamp_ns"] >= acquired_at.get(key, 2**63 - 1):
                dropout_rows.append((row["source_timestamp_ns"], duration, is_observed))
        total_eligible_ns += target_total
        observed_ns += target_observed
        continuity_ns += target_continuity
        label = f"{key[0]}:{key[1]}"
        coverage_by_target[label] = {
            "eligible_duration_ms": target_total / 1_000_000,
            "observed_coverage": target_observed / target_total if target_total else 0,
            "continuity": target_continuity / target_total if target_total else 0,
        }
        dropout_durations_ns.extend(
            _dropout_intervals(dropout_rows, thresholds.visible_dropout_tolerance_ms * 1_000_000)
        )
    dropout_ms = [duration / 1_000_000 for duration in dropout_durations_ns]
    target_minutes = total_eligible_ns / 60_000_000_000
    coverage = {
        "overall_observed": observed_ns / total_eligible_ns if total_eligible_ns else None,
        "overall_continuity": continuity_ns / total_eligible_ns if total_eligible_ns else None,
        "eligible_target_time_ms": total_eligible_ns / 1_000_000,
        "per_target": coverage_by_target,
        "by_visibility": {
            key: values[0] / values[1] if values[1] else None for key, values in sorted(coverage_by_visibility.items())
        },
        "by_scenario": {
            key: values[0] / values[1] if values[1] else None for key, values in sorted(coverage_by_scenario.items())
        },
    }
    dropouts = _summary(dropout_ms)
    dropouts.update(
        {
            "count": len(dropout_ms),
            "per_target_minute": len(dropout_ms) / target_minutes if target_minutes else None,
            "exceeding_ms": {
                str(deadline): sum(duration > deadline for duration in dropout_ms) for deadline in (100, 250, 500, 1000)
            },
        }
    )

    localization_rows: list[dict[str, Any]] = []
    size_groups: dict[str, list[float]] = defaultdict(list)
    visibility_groups: dict[str, list[float]] = defaultdict(list)
    for match in observed_matches:
        gt_box = match.gt["bbox_xyxy"]
        output_box = match.output["geometry"]["value"]
        width_error = (output_box[2] - output_box[0]) - (gt_box[2] - gt_box[0])
        height_error = (output_box[3] - output_box[1]) - (gt_box[3] - gt_box[1])
        diagonal = math.hypot(gt_box[2] - gt_box[0], gt_box[3] - gt_box[1])
        area = (gt_box[2] - gt_box[0]) * (gt_box[3] - gt_box[1])
        size = "small" if area < 32**2 else ("medium" if area < 96**2 else "large")
        size_groups[size].append(match.iou)
        visibility_fraction = match.gt.get("visibility_fraction")
        if visibility_fraction is not None:
            visibility = (
                "full" if visibility_fraction >= 0.8 else ("partial" if visibility_fraction >= 0.3 else "low")
            )
            visibility_groups[visibility].append(match.iou)
        localization_rows.append(
            {
                "iou": match.iou,
                "center_error_px": match.center_error_px,
                "normalized_center_error": match.center_error_px / diagonal if diagonal else 0,
                "width_error_px": width_error,
                "height_error_px": height_error,
            }
        )
    localization = {
        "sample_count": len(localization_rows),
        "mean_iou": statistics.fmean(row["iou"] for row in localization_rows) if localization_rows else None,
        "median_iou": percentile((row["iou"] for row in localization_rows), 0.5),
        "center_error_px": _summary([row["center_error_px"] for row in localization_rows]),
        "normalized_center_error": _summary([row["normalized_center_error"] for row in localization_rows]),
        "mean_width_error_px": statistics.fmean(row["width_error_px"] for row in localization_rows)
        if localization_rows
        else None,
        "mean_height_error_px": statistics.fmean(row["height_error_px"] for row in localization_rows)
        if localization_rows
        else None,
        "mean_iou_by_visibility": {key: statistics.fmean(values) for key, values in sorted(visibility_groups.items())},
        "mean_iou_by_target_size": {key: statistics.fmean(values) for key, values in sorted(size_groups.items())},
    }

    tracks_by_target: dict[tuple[str, str], list[Match]] = defaultdict(list)
    for match in matches:
        tracks_by_target[(match.sequence_id, match.target_id)].append(match)
    observed_tracks_by_target: dict[tuple[str, str], list[Match]] = defaultdict(list)
    for match in observed_matches:
        observed_tracks_by_target[(match.sequence_id, match.target_id)].append(match)
    id_switches = fragments = 0
    for target_matches in observed_tracks_by_target.values():
        target_matches.sort(key=lambda match: match.source_timestamp_ns)
        previous_id: str | None = None
        previous_timestamp: int | None = None
        for match in target_matches:
            if previous_id is not None and match.track_id != previous_id:
                id_switches += 1
            if previous_timestamp is not None:
                expected = durations.get((match.sequence_id, previous_timestamp), 0)
                if match.source_timestamp_ns > previous_timestamp + expected:
                    fragments += 1
            previous_id = match.track_id
            previous_timestamp = match.source_timestamp_ns
    overlapping_by_frame_target: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    duplicate_ids_by_target: dict[tuple[str, str], set[str]] = defaultdict(set)
    for output in scored_unmatched:
        if output.get("event") not in TRACK_OBSERVATION_EVENTS:
            continue
        frame_key = (output["sequence_id"], output["source_timestamp_ns"])
        for gt in ground_truth_by_frame.get(frame_key, []):
            if (
                gt.get("on_screen")
                and gt.get("bbox_xyxy")
                and center_error(gt["bbox_xyxy"], output["geometry"]["value"]) <= thresholds.max_match_center_error_px
            ):
                frame_key = (gt["sequence_id"], gt["source_timestamp_ns"], gt["target_id"])
                overlapping_by_frame_target[frame_key].add(output["track_id"])
                duplicate_ids_by_target[(gt["sequence_id"], gt["target_id"])].add(output["track_id"])
    matched_target_frames = {
        (match.sequence_id, match.source_timestamp_ns, match.target_id) for match in observed_matches
    }
    duplicate_tracks = len(
        {
            track_id
            for key, track_ids in overlapping_by_frame_target.items()
            if key in matched_target_frames
            for track_id in track_ids
        }
    )
    merges = 0
    for output in scored_outputs:
        if output.get("event") not in TRACK_OBSERVATION_EVENTS:
            continue
        frame_key = (output["sequence_id"], output["source_timestamp_ns"])
        overlapping_targets = sum(
            gt.get("on_screen")
            and gt.get("bbox_xyxy") is not None
            and bbox_iou(gt["bbox_xyxy"], output["geometry"]["value"]) >= thresholds.minimum_match_iou
            for gt in ground_truth_by_frame.get(frame_key, [])
        )
        if overlapping_targets > 1:
            merges += 1
    splits = sum(
        bool(track_ids) for key, track_ids in overlapping_by_frame_target.items() if key in matched_target_frames
    )
    identity = {
        "id_switches": id_switches,
        "track_fragmentation": fragments,
        "duplicate_tracks": duplicate_tracks,
        "duplicate_tracks_per_real_target": {
            f"{key[0]}:{key[1]}": len(track_ids) for key, track_ids in sorted(duplicate_ids_by_target.items())
        },
        "track_merges": merges,
        "track_splits": splits,
        "id_switches_per_target_minute": id_switches / target_minutes if target_minutes else None,
    }

    neutral_ignored = [
        record
        for record in unmatched
        if record.get("neutral_ignored") and record.get("event") in TRACK_OBSERVATION_EVENTS
    ]
    unmatched_tracks = [
        record
        for record in unmatched
        if record.get("event") in TRACK_OBSERVATION_EVENTS and not record.get("neutral_ignored")
    ]
    false_by_id: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in unmatched_tracks:
        false_by_id[(record["sequence_id"], record["track_id"])].append(record)
    false_durations_ms: list[float] = []
    one_frame_false_detections = 0
    for records in false_by_id.values():
        records.sort(key=lambda record: record["source_timestamp_ns"])
        start, end = records[0]["source_timestamp_ns"], records[-1]["source_timestamp_ns"]
        step = durations.get((records[-1]["sequence_id"], end), 0)
        false_durations_ms.append((end - start + step) / 1_000_000)
        if len(records) == 1:
            one_frame_false_detections += 1
    persistent_false_tracks = sum(
        duration >= thresholds.confirmed_track_min_duration_ms for duration in false_durations_ms
    )
    sequence_duration_ns: dict[str, int] = defaultdict(int)
    for (sequence, _timestamp), duration in durations.items():
        sequence_duration_ns[sequence] += duration
    camera_minutes = sum(sequence_duration_ns.values()) / 60_000_000_000
    camera_hours = camera_minutes / 60
    false_track_seconds = sum(false_durations_ms) / 1000
    false_detections = {
        "detections": len(unmatched_tracks),
        "one_frame_false_detections": one_frame_false_detections,
        "detections_per_camera_minute": len(unmatched_tracks) / camera_minutes if camera_minutes else None,
        "track_births": len(false_by_id),
        "persistent_track_births": persistent_false_tracks,
        "track_births_per_camera_hour": len(false_by_id) / camera_hours if camera_hours else None,
        "track_duration_ms": _summary(false_durations_ms),
        "false_track_seconds_per_camera_hour": false_track_seconds / camera_hours if camera_hours else None,
        "longest_lived_false_track_ms": max(false_durations_ms) if false_durations_ms else 0,
        "high_confidence_false_detections": sum(
            float(record["confidence"]) >= thresholds.high_confidence_threshold for record in unmatched_tracks
        ),
        "by_scenario": dict(
            sorted(
                Counter(scenario_families.get(record["sequence_id"], "unknown") for record in unmatched_tracks).items()
            )
        ),
        "neutral_ignored_predictions": len(neutral_ignored),
        "neutral_ignored_by_scenario": dict(
            sorted(
                Counter(scenario_families.get(record["sequence_id"], "unknown") for record in neutral_ignored).items()
            )
        ),
        "neutral_ignored_annotation_ids": sorted(
            {
                annotation_id
                for record in neutral_ignored
                for annotation_id in record.get("ignore_annotation_ids", [])
            }
        ),
    }

    reacquisition_rows: list[dict[str, Any]] = []
    for key, rows in sorted(all_by_target.items()):
        rows.sort(key=lambda row: row["source_timestamp_ns"])
        prior_match: Match | None = None
        had_eligible_observation = False
        in_gap = False
        gap_start = 0
        for row in rows:
            current_match = observed_match_by_target_frame.get(
                (row["sequence_id"], row["source_timestamp_ns"], row["target_id"])
            )
            eligible = row["on_screen"] and row["eligible_for_detection"]
            if not eligible and prior_match and had_eligible_observation:
                if not in_gap:
                    gap_start = row["source_timestamp_ns"]
                in_gap = True
            elif eligible and in_gap:
                after = [
                    match
                    for match in observed_tracks_by_target.get(key, [])
                    if match.source_timestamp_ns >= row["source_timestamp_ns"]
                ]
                reacquired = min(after, key=lambda match: match.source_timestamp_ns) if after else None
                gap_matches = [
                    match
                    for match in tracks_by_target.get(key, [])
                    if gap_start <= match.source_timestamp_ns < row["source_timestamp_ns"]
                ]
                gap_confidences = [float(match.output["confidence"]) for match in gap_matches]
                pre_gap_assignments = {
                    other_key[1]: max(
                        (
                            match
                            for match in target_matches
                            if match.source_timestamp_ns < gap_start
                        ),
                        key=lambda match: match.source_timestamp_ns,
                        default=None,
                    )
                    for other_key, target_matches in observed_tracks_by_target.items()
                    if other_key[0] == key[0]
                }
                swapped_with = next(
                    (
                        target_id
                        for target_id, match in sorted(pre_gap_assignments.items())
                        if target_id != key[1]
                        and match is not None
                        and reacquired is not None
                        and match.track_id == reacquired.track_id
                    ),
                    None,
                )
                reacquisition_rows.append(
                    {
                        "sequence_id": key[0],
                        "target_id": key[1],
                        "gap_duration_ms": (row["source_timestamp_ns"] - gap_start) / 1_000_000,
                        "full_occlusion": True,
                        "correct_target": reacquired is not None and swapped_with is None,
                        "same_id": bool(reacquired and prior_match and reacquired.track_id == prior_match.track_id),
                        "track_remained_active": bool(gap_matches),
                        "same_id_preserved_during_gap": bool(
                            gap_matches
                            and prior_match
                            and all(match.track_id == prior_match.track_id for match in gap_matches)
                        ),
                        "gap_position_error_px": _summary([match.center_error_px for match in gap_matches]),
                        "confidence_decreased": bool(
                            len(gap_confidences) >= 2 and gap_confidences[-1] < gap_confidences[0]
                        ),
                        "wrong_target_association": swapped_with is not None,
                        "swapped_with_target_id": swapped_with,
                        "latency_ms": (
                            (reacquired.source_timestamp_ns - row["source_timestamp_ns"]) / 1_000_000
                            if reacquired
                            else None
                        ),
                    }
                )
                in_gap = False
            if current_match:
                prior_match = current_match
                if eligible:
                    had_eligible_observation = True
    reacq_latencies = [row["latency_ms"] for row in reacquisition_rows if row["latency_ms"] is not None]
    reacquisition = {
        "events": len(reacquisition_rows),
        "same_id_rate": sum(row["same_id"] for row in reacquisition_rows) / len(reacquisition_rows)
        if reacquisition_rows
        else None,
        "correct_target_rate": sum(row["correct_target"] for row in reacquisition_rows) / len(reacquisition_rows)
        if reacquisition_rows
        else None,
        "latency_ms": _summary(reacq_latencies),
        "by_gap": reacquisition_rows,
        "after_full_occlusion_rate": sum(row["correct_target"] for row in reacquisition_rows) / len(reacquisition_rows)
        if reacquisition_rows
        else None,
    }

    timed_outputs: list[tuple[CollectedRecord, float]] = []
    native_source_offsets_ms: list[float] = []
    for item in collected:
        record = item.system_record
        if record.get("event") not in TRACK_EVENTS:
            continue
        source_timestamp_ns = int(record["source_timestamp_ns"])
        if frame_delivery_ns is None:
            if item.collector_received_timestamp_ns < source_timestamp_ns:
                continue
            latency_origin_ns = source_timestamp_ns
        else:
            latency_origin_ns = frame_delivery_ns.get(
                (str(record["sequence_id"]), source_timestamp_ns)
            )
            if latency_origin_ns is None:
                continue
            native_source_offsets_ms.append(
                (item.collector_received_timestamp_ns - source_timestamp_ns)
                / 1_000_000
            )
        timed_outputs.append(
            (
                item,
                max(
                    0.0,
                    (item.collector_received_timestamp_ns - latency_origin_ns)
                    / 1_000_000,
                ),
            )
        )
    latency_ms = [value for _item, value in timed_outputs]
    latency_lookup = {id(item.system_record): value for item, value in timed_outputs}
    latency = _summary(latency_ms)
    latency.update(
        {
            "deadline_ms": thresholds.latency_deadline_ms,
            "deadline_miss_rate": sum(value > thresholds.latency_deadline_ms for value in latency_ms) / len(latency_ms)
            if latency_ms
            else None,
            "first_tentative_ms": min(
                (value for item, value in timed_outputs if item.system_record.get("state") == "tentative"),
                default=None,
            ),
            "first_confirmed_ms": min(
                (
                    value
                    for item, value in timed_outputs
                    if item.system_record.get("state") in {"confirmed", "reacquired"}
                ),
                default=None,
            ),
            "over_time_ms": latency_ms,
            "clock": (
                "successful_frame_delivery_completion"
                if frame_delivery_ns is not None
                else "source_timestamp_compatibility"
            ),
            "native_source_offset_ms": (
                _summary(native_source_offsets_ms)
                if frame_delivery_ns is not None
                else None
            ),
        }
    )
    if len(latency_ms) >= 2:
        midpoint = len(latency_ms) // 2
        first_half = percentile(latency_ms[:midpoint], 0.5)
        second_half = percentile(latency_ms[midpoint:], 0.5)
        latency["drift_ms"] = second_half - first_half if first_half is not None and second_half is not None else None
    else:
        latency["drift_ms"] = None

    count_by_frame: Counter[tuple[str, int]] = Counter(
        (gt["sequence_id"], gt["source_timestamp_ns"])
        for gt in ground_truth
        if not gt.get("ignore", False) and gt["on_screen"] and gt["eligible_for_detection"]
    )
    group_data: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for gt in ground_truth:
        if gt.get("ignore", False):
            continue
        if not gt["on_screen"] or not gt["eligible_for_detection"]:
            continue
        count = count_by_frame[(gt["sequence_id"], gt["source_timestamp_ns"])]
        group = "1" if count == 1 else ("2" if count == 2 else ("4" if count <= 4 else "8+"))
        group_data[group][1] += 1
        if (gt["sequence_id"], gt["source_timestamp_ns"], gt["target_id"]) in observed_match_by_target_frame:
            group_data[group][0] += 1
    multi_target = {
        group: {"matched": values[0], "eligible": values[1], "coverage": values[0] / values[1]}
        for group, values in sorted(group_data.items())
    }
    latency_by_count: dict[str, list[float]] = defaultdict(list)
    fault_latency: list[float] = []
    for item in collected:
        record = item.system_record
        if record.get("event") not in TRACK_EVENTS:
            continue
        value = latency_lookup.get(id(record))
        if value is None:
            continue
        count = count_by_frame.get((record["sequence_id"], record["source_timestamp_ns"]), 0)
        group = "0" if count == 0 else ("1" if count == 1 else ("2" if count == 2 else ("4" if count <= 4 else "8+")))
        latency_by_count[group].append(value)
        if (record["sequence_id"], record["source_timestamp_ns"]) in fault_timestamps:
            fault_latency.append(value)
    latency["by_target_count"] = {group: _summary(values) for group, values in sorted(latency_by_count.items())}
    latency["under_fault_injection"] = _summary(fault_latency)
    if sequence_timestamps:
        total_camera_ns = 0
        for timestamps_for_sequence in sequence_timestamps.values():
            ordered = sorted(timestamps_for_sequence)
            steps = [right - left for left, right in zip(ordered, ordered[1:], strict=False)]
            total_camera_ns += (
                (ordered[-1] - ordered[0] + (int(statistics.median(steps)) if steps else 0)) if ordered else 0
            )
        camera_minutes = total_camera_ns / 60_000_000_000
        camera_hours = camera_minutes / 60
        false_detections["detections_per_camera_minute"] = (
            len(unmatched_tracks) / camera_minutes if camera_minutes else None
        )
        false_detections["track_births_per_camera_hour"] = len(false_by_id) / camera_hours if camera_hours else None
        false_detections["false_track_seconds_per_camera_hour"] = (
            false_track_seconds / camera_hours if camera_hours else None
        )
    camera_seconds = camera_minutes * 60
    latency["output_update_rate_hz"] = len(latency_ms) / camera_seconds if camera_seconds else None

    robustness = {
        "occlusion_survival": {
            "events": len(reacquisition_rows),
            "track_active_rate": sum(row["track_remained_active"] for row in reacquisition_rows)
            / len(reacquisition_rows)
            if reacquisition_rows
            else None,
            "same_id_preserved_rate": sum(row["same_id_preserved_during_gap"] for row in reacquisition_rows)
            / len(reacquisition_rows)
            if reacquisition_rows
            else None,
            "wrong_target_associations": sum(row["wrong_target_association"] for row in reacquisition_rows),
            "by_gap": reacquisition_rows,
        }
    }
    timestamps_by_sequence: dict[str, list[int]] = defaultdict(list)
    for gt_row in ground_truth:
        timestamps_by_sequence[gt_row["sequence_id"]].append(gt_row["source_timestamp_ns"])
    fault_recovery: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (sequence, timestamp), kinds in sorted(fault_events.items()):
        later = sorted(value for value in set(timestamps_by_sequence[sequence]) if value > timestamp)
        recovery_timestamp = later[0] if later else None
        eligible_targets = {
            gt_row["target_id"]
            for gt_row in ground_truth
            if recovery_timestamp is not None
            and gt_row["sequence_id"] == sequence
            and gt_row["source_timestamp_ns"] == recovery_timestamp
            and gt_row["on_screen"]
            and gt_row["eligible_for_detection"]
        }
        recovery_matches = [
            match
            for match in matches
            if recovery_timestamp is not None
            and match.sequence_id == sequence
            and match.source_timestamp_ns == recovery_timestamp
            and match.target_id in eligible_targets
            and match.output["support"] == "observed"
        ]
        before_matches = {
            match.target_id: match.track_id
            for match in matches
            if match.sequence_id == sequence and match.source_timestamp_ns <= timestamp
        }
        same_id = sum(before_matches.get(match.target_id) == match.track_id for match in recovery_matches)
        for kind in kinds:
            fault_recovery[kind].append(
                {
                    "sequence_id": sequence,
                    "fault_timestamp_ns": timestamp,
                    "recovery_timestamp_ns": recovery_timestamp,
                    "eligible_targets": len(eligible_targets),
                    "observed_recoveries": len(recovery_matches),
                    "same_id_recoveries": same_id,
                }
            )
    robustness["feed_faults"] = {
        kind: {
            "events": len(rows),
            "observed_recovery_rate": (
                sum(row["observed_recoveries"] for row in rows) / sum(row["eligible_targets"] for row in rows)
                if sum(row["eligible_targets"] for row in rows)
                else None
            ),
            "same_id_recovery_rate": (
                sum(row["same_id_recoveries"] for row in rows) / sum(row["eligible_targets"] for row in rows)
                if sum(row["eligible_targets"] for row in rows)
                else None
            ),
            "details": rows,
        }
        for kind, rows in sorted(fault_recovery.items())
    }
    interruption = robustness["feed_faults"].get("feed_interruption", {})
    blackout = robustness["feed_faults"].get("blackout", {})
    reacquisition["after_feed_interruption_rate"] = interruption.get("observed_recovery_rate")
    reacquisition["after_visible_detector_dropout_rate"] = blackout.get("observed_recovery_rate")

    track_records = [record for record in scored_outputs if record.get("event") in TRACK_EVENTS]
    sequences_by_track: dict[str, set[str]] = defaultdict(set)
    for record in track_records:
        sequences_by_track[str(record["track_id"])].add(str(record["sequence_id"]))
    contaminated_ids = sorted(track_id for track_id, sequences in sequences_by_track.items() if len(sequences) > 1)
    lifecycle = _track_id_lifecycle(track_records, observed_matches)
    sequence_order = list(dict.fromkeys(row["sequence_id"] for row in ground_truth))
    midpoint = max(1, len(sequence_order) // 2)
    first_sequences, second_sequences = set(sequence_order[:midpoint]), set(sequence_order[midpoint:])

    def false_rate(sequences: set[str]) -> float | None:
        duration_ns = sum(sequence_duration_ns.get(sequence, 0) for sequence in sequences)
        count = sum(record["sequence_id"] in sequences for record in unmatched_tracks)
        return count / (duration_ns / 60_000_000_000) if duration_ns else None

    first_false_rate = false_rate(first_sequences)
    second_false_rate = false_rate(second_sequences)
    false_accumulation = (
        second_false_rate - first_false_rate
        if first_false_rate is not None and second_false_rate is not None
        else None
    )
    long_running_stability = {
        **lifecycle,
        "track_id_exhaustion_detected": lifecycle["track_id_reuse_detected"],
        "state_contamination_events": len(contaminated_ids),
        "state_contaminated_track_ids": contaminated_ids,
        "false_positive_rate_first_half_per_camera_minute": first_false_rate,
        "false_positive_rate_second_half_per_camera_minute": second_false_rate,
        "false_positive_accumulation_per_camera_minute": false_accumulation,
        "interruption_recovery_rate": interruption.get("observed_recovery_rate"),
        "latency_drift_ms": latency.get("drift_ms"),
    }
    mot = _calculate_mot_metrics(ground_truth, scored_outputs, scenario_families)

    return {
        "acquisition": acquisition,
        "coverage": coverage,
        "visible_dropouts": dropouts,
        "localization": localization,
        "identity": identity,
        "false_detections": false_detections,
        "reacquisition": reacquisition,
        "robustness": robustness,
        "latency": latency,
        "multi_target": multi_target,
        "multi_object_tracking": mot,
        "long_running_stability": long_running_stability,
        "sample_counts": {
            "ground_truth_records": len(ground_truth),
            "output_records": len(outputs),
            "matches": len(observed_matches),
            "continuity_matches": len(matches),
            "neutral_ignored_predictions": len(neutral_ignored),
        },
    }, matches
