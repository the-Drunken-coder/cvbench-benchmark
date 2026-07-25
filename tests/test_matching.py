from cvbench.config import Thresholds
from cvbench.matching import bbox_iou, center_error, match_records, match_records_by_support
from tests.helpers import gt, output


def test_iou_and_center_error_are_hand_calculated() -> None:
    assert bbox_iou([0, 0, 10, 10], [5, 0, 15, 10]) == 1 / 3
    assert center_error([0, 0, 10, 10], [6, 8, 16, 18]) == 10


def test_hungarian_matching_finds_global_assignment() -> None:
    ground_truth = [gt(0, target="a", box=[0, 0, 10, 10]), gt(0, target="b", box=[20, 0, 30, 10])]
    records = [
        output(0, track="left", box=[1, 0, 11, 10]).system_record,
        output(0, track="right", box=[19, 0, 29, 10]).system_record,
    ]
    matches, unmatched = match_records(ground_truth, records, Thresholds(max_match_center_error_px=30))
    assert [(match.target_id, match.track_id) for match in matches] == [("a", "left"), ("b", "right")]
    assert unmatched == []


def test_matching_is_stable_under_input_reordering() -> None:
    ground_truth = [gt(0, target="b"), gt(0, target="a")]
    records = [output(0, track="z").system_record, output(0, track="a").system_record]
    first, _ = match_records(ground_truth, records, Thresholds())
    second, _ = match_records(list(reversed(ground_truth)), list(reversed(records)), Thresholds())
    assert [(m.target_id, m.track_id) for m in first] == [(m.target_id, m.track_id) for m in second]


def test_class_gate_can_be_configured() -> None:
    record = output(0).system_record
    record["class_id"] = "vehicle"
    strict, unmatched = match_records([gt(0)], [record], Thresholds())
    agnostic, _ = match_records([gt(0)], [record], Thresholds(class_agnostic=True))
    assert strict == [] and unmatched == [record]
    assert len(agnostic) == 1


def test_class_agnostic_ignore_region_neutralizes_any_output_class() -> None:
    ignored = {
        **gt(0, target="ignore", box=[0, 0, 20, 20]),
        "class_id": "__ignore__",
        "ignore": True,
        "ignore_region": True,
        "ignore_class_agnostic": True,
        "ignore_region_id": "ignore",
    }
    record = output(0, box=[1, 1, 19, 19]).system_record

    observed_matches, continuity_matches, unmatched = match_records_by_support(
        [ignored], [record], Thresholds()
    )

    assert observed_matches == []
    assert continuity_matches == []
    assert unmatched[0]["neutral_ignored"] is True


def test_observation_is_matched_independently_from_better_prediction() -> None:
    observed = output(0, track="observed", box=[1, 0, 11, 10]).system_record
    predicted = output(
        0, track="predicted", box=[0, 0, 10, 10], support="predicted", state="coasting"
    ).system_record
    observed_matches, continuity_matches, unmatched_observed = match_records_by_support(
        [gt(0, box=[0, 0, 10, 10])], [predicted, observed], Thresholds()
    )
    assert [match.track_id for match in observed_matches] == ["observed"]
    assert [match.track_id for match in continuity_matches] == ["observed"]
    assert unmatched_observed == []
