from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .config import Thresholds
from .model import Match
from .protocol import TRACK_OBSERVATION_EVENTS

INVALID_COST = 1_000_000.0


def bbox_iou(left: list[float], right: list[float]) -> float:
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def intersection_over_prediction_area(prediction: list[float], region: list[float]) -> float:
    """Return the fraction of a prediction covered by an ignore region."""
    intersection_width = max(0.0, min(prediction[2], region[2]) - max(prediction[0], region[0]))
    intersection_height = max(0.0, min(prediction[3], region[3]) - max(prediction[1], region[1]))
    prediction_area = max(0.0, prediction[2] - prediction[0]) * max(0.0, prediction[3] - prediction[1])
    if prediction_area <= 0:
        return 0.0
    return intersection_width * intersection_height / prediction_area


def center_error(left: list[float], right: list[float]) -> float:
    left_center = ((left[0] + left[2]) / 2, (left[1] + left[3]) / 2)
    right_center = ((right[0] + right[2]) / 2, (right[1] + right[3]) / 2)
    return math.hypot(left_center[0] - right_center[0], left_center[1] - right_center[1])


def _target_gate_matches(target: dict[str, Any], output: dict[str, Any], thresholds: Thresholds) -> bool:
    if not thresholds.class_agnostic and target.get("class_id") != output.get("class_id"):
        return False
    prediction_box = output["geometry"]["value"]
    return bbox_iou(target["bbox_xyxy"], prediction_box) >= thresholds.minimum_match_iou or center_error(
        target["bbox_xyxy"], prediction_box
    ) <= thresholds.max_match_center_error_px


def _hungarian(cost: list[list[float]]) -> list[tuple[int, int]]:
    """Return a deterministic minimum-cost assignment for a rectangular matrix."""
    if not cost or not cost[0]:
        return []
    rows, columns = len(cost), len(cost[0])
    transposed = rows > columns
    matrix = [list(row) for row in cost]
    if transposed:
        matrix = [[cost[row][column] for row in range(rows)] for column in range(columns)]
    n, m = len(matrix), len(matrix[0])
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for row in range(1, n + 1):
        p[0] = row
        column0 = 0
        min_values = [math.inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = math.inf
            column1 = 0
            for column in range(1, m + 1):
                if used[column]:
                    continue
                current = matrix[row0 - 1][column - 1] - u[row0] - v[column]
                if current < min_values[column]:
                    min_values[column] = current
                    way[column] = column0
                if min_values[column] < delta:
                    delta = min_values[column]
                    column1 = column
            for column in range(m + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    min_values[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment: list[tuple[int, int]] = []
    for column in range(1, m + 1):
        if p[column]:
            pair = (p[column] - 1, column - 1)
            assignment.append((pair[1], pair[0]) if transposed else pair)
    return sorted(assignment)


def match_records(
    ground_truth: list[dict[str, Any]], outputs: list[dict[str, Any]], thresholds: Thresholds
) -> tuple[list[Match], list[dict[str, Any]]]:
    gt_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    output_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for gt in ground_truth:
        if not gt.get("ignore", False) and gt.get("on_screen") and gt.get("bbox_xyxy"):
            gt_by_frame[(gt["sequence_id"], gt["source_timestamp_ns"])].append(gt)
    for output in outputs:
        if output.get("event") in TRACK_OBSERVATION_EVENTS | {"track_ended"}:
            output_by_frame[(output["sequence_id"], output["source_timestamp_ns"])].append(output)
    matches: list[Match] = []
    unmatched: list[dict[str, Any]] = []
    keys = sorted(set(gt_by_frame) | set(output_by_frame))
    for key in keys:
        targets = sorted(gt_by_frame[key], key=lambda item: item["target_id"])
        records = sorted(output_by_frame[key], key=lambda item: (item["track_id"], item["event"]))
        if not targets:
            unmatched.extend(records)
            continue
        if not records:
            continue
        costs: list[list[float]] = []
        pair_data: dict[tuple[int, int], tuple[float, float]] = {}
        for target_index, target in enumerate(targets):
            row: list[float] = []
            for output_index, output in enumerate(records):
                iou = bbox_iou(target["bbox_xyxy"], output["geometry"]["value"])
                distance = center_error(target["bbox_xyxy"], output["geometry"]["value"])
                class_ok = thresholds.class_agnostic or target["class_id"] == output["class_id"]
                gated = class_ok and (
                    iou >= thresholds.minimum_match_iou or distance <= thresholds.max_match_center_error_px
                )
                pair_data[(target_index, output_index)] = (iou, distance)
                tie_break = target_index * 1e-9 + output_index * 1e-12
                row.append((1.0 - iou) + distance / 1_000_000 + tie_break if gated else INVALID_COST)
            costs.append(row)
        used_outputs: set[int] = set()
        for target_index, output_index in _hungarian(costs):
            if costs[target_index][output_index] >= INVALID_COST:
                continue
            target = targets[target_index]
            output = records[output_index]
            iou, distance = pair_data[(target_index, output_index)]
            used_outputs.add(output_index)
            matches.append(
                Match(
                    sequence_id=key[0],
                    source_timestamp_ns=key[1],
                    target_id=target["target_id"],
                    track_id=output["track_id"],
                    gt=target,
                    output=output,
                    iou=iou,
                    center_error_px=distance,
                )
            )
        unmatched.extend(record for index, record in enumerate(records) if index not in used_outputs)
    return matches, unmatched


def mark_ignored_outputs(
    ground_truth: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    thresholds: Thresholds,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mark unmatched observed outputs neutral when they overlap an ignore box.

    Scoreable targets have already been matched by ``match_records``.  This
    second pass therefore cannot let an ignore annotation steal a real target.
    """
    ignores_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    targets_by_frame: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in ground_truth:
        if row.get("ignore") and row.get("on_screen") and row.get("bbox_xyxy"):
            ignores_by_frame[(row["sequence_id"], row["source_timestamp_ns"])].append(row)
        elif row.get("on_screen") and row.get("bbox_xyxy"):
            targets_by_frame[(row["sequence_id"], row["source_timestamp_ns"])].append(row)
    neutral: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for output in outputs:
        if output.get("event") not in TRACK_OBSERVATION_EVENTS:
            remaining.append(output)
            continue
        target_candidates = targets_by_frame.get((output["sequence_id"], output["source_timestamp_ns"]), [])
        if any(_target_gate_matches(target, output, thresholds) for target in target_candidates):
            remaining.append(output)
            continue
        candidates = ignores_by_frame.get((output["sequence_id"], output["source_timestamp_ns"]), [])
        prediction_box = output["geometry"]["value"]
        overlaps = []
        for row in candidates:
            if not thresholds.class_agnostic and row.get("class_id") != output.get("class_id"):
                continue
            overlap = (
                intersection_over_prediction_area(prediction_box, row["bbox_xyxy"])
                if row.get("ignore_region")
                else bbox_iou(row["bbox_xyxy"], prediction_box)
            )
            if overlap >= thresholds.ignore_match_iou:
                overlaps.append(row)
        if overlaps:
            marked = dict(output)
            marked["neutral_ignored"] = True
            marked["_neutral_output_identity"] = id(output)
            marked["ignore_annotation_ids"] = sorted(row["target_id"] for row in overlaps)
            neutral.append(marked)
        else:
            remaining.append(output)
    return remaining, neutral


def match_records_by_support(
    ground_truth: list[dict[str, Any]], outputs: list[dict[str, Any]], thresholds: Thresholds
) -> tuple[list[Match], list[Match], list[dict[str, Any]]]:
    """Match observations independently, then fill continuity gaps with predictions.

    A predicted record must never suppress a valid observation. False-detection
    accounting therefore uses only unmatched observed records.
    """
    observed = [record for record in outputs if record.get("support") == "observed"]
    predicted = [record for record in outputs if record.get("support") == "predicted"]
    observed_matches, unmatched_observed = match_records(ground_truth, observed, thresholds)
    unmatched_observed, neutral = mark_ignored_outputs(ground_truth, unmatched_observed, thresholds)
    unmatched_observed.extend(neutral)
    observed_keys = {
        (match.sequence_id, match.source_timestamp_ns, match.target_id) for match in observed_matches
    }
    remaining_ground_truth = [
        row
        for row in ground_truth
        if (row["sequence_id"], row["source_timestamp_ns"], row["target_id"]) not in observed_keys
    ]
    predicted_matches, _ = match_records(remaining_ground_truth, predicted, thresholds)
    continuity_matches = [*observed_matches, *predicted_matches]
    return observed_matches, continuity_matches, unmatched_observed
