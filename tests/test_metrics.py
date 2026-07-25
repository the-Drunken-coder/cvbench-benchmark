import pytest

from cvbench.config import Thresholds
from cvbench.matching import intersection_over_prediction_area
from cvbench.metrics import calculate_metrics, percentile
from tests.helpers import gt, output


def test_acquisition_latency_is_exactly_250_ms() -> None:
    ground_truth = [gt(1_000_000_000), gt(1_250_000_000), gt(1_500_000_000)]
    records = [output(1_250_000_000)]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    assert metrics["acquisition"]["median"] == 250
    assert metrics["acquisition"]["per_target_latency_ms"]["seq:gt-1"] == 250
    assert metrics["acquisition"]["within_deadline"]["250"] == 1


def test_observed_coverage_and_continuity_are_separate() -> None:
    timestamps = [0, 100_000_000, 200_000_000, 300_000_000, 400_000_000]
    ground_truth = [gt(timestamp) for timestamp in timestamps]
    records = [
        output(0),
        output(100_000_000),
        output(200_000_000, support="predicted", state="coasting"),
        output(400_000_000),
    ]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds(visible_dropout_tolerance_ms=100))
    assert metrics["coverage"]["overall_observed"] == 0.6
    assert metrics["coverage"]["overall_continuity"] == 0.8
    assert metrics["visible_dropouts"]["count"] == 1
    assert metrics["visible_dropouts"]["maximum"] == 200


def test_better_prediction_cannot_suppress_valid_observation() -> None:
    observed = output(0, track="observed", box=[1, 0, 11, 10])
    predicted = output(
        0, track="predicted", box=[0, 0, 10, 10], support="predicted", state="coasting"
    )
    metrics, _ = calculate_metrics(
        [gt(0, box=[0, 0, 10, 10]), gt(100_000_000, box=[0, 0, 10, 10])],
        [predicted, observed, output(100_000_000, track="observed", box=[1, 0, 11, 10])],
        Thresholds(),
    )
    assert metrics["coverage"]["overall_observed"] == 1
    assert metrics["coverage"]["overall_continuity"] == 1
    assert metrics["false_detections"]["detections"] == 0


def test_reacquisition_latency_is_exactly_180_ms_and_same_id() -> None:
    ground_truth = [
        gt(1_000_000_000),
        gt(1_100_000_000, eligible=False, visibility=0, occlusion="full"),
        gt(1_200_000_000, eligible=False, visibility=0, occlusion="full"),
        gt(1_300_000_000),
        gt(1_480_000_000),
        gt(1_580_000_000),
    ]
    records = [
        output(1_000_000_000, track="same"),
        output(1_100_000_000, track="same", support="predicted", state="coasting"),
        output(1_200_000_000, track="same", support="predicted", state="coasting"),
        output(1_480_000_000, track="same", state="reacquired"),
    ]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    assert metrics["reacquisition"]["latency_ms"]["median"] == 180
    assert metrics["reacquisition"]["same_id_rate"] == 1
    assert metrics["robustness"]["occlusion_survival"]["track_active_rate"] == 1


def test_post_occlusion_swapped_identities_are_wrong_target_associations() -> None:
    ground_truth = [
        gt(0, target="left", box=[0, 0, 10, 10]),
        gt(0, target="right", box=[100, 0, 110, 10]),
        gt(100_000_000, target="left", box=[0, 0, 10, 10], eligible=False, visibility=0, occlusion="full"),
        gt(100_000_000, target="right", box=[100, 0, 110, 10], eligible=False, visibility=0, occlusion="full"),
        gt(200_000_000, target="left", box=[0, 0, 10, 10]),
        gt(200_000_000, target="right", box=[100, 0, 110, 10]),
    ]
    records = [
        output(0, track="left-id", box=[0, 0, 10, 10]),
        output(0, track="right-id", box=[100, 0, 110, 10]),
        output(200_000_000, track="right-id", box=[0, 0, 10, 10], state="reacquired"),
        output(200_000_000, track="left-id", box=[100, 0, 110, 10], state="reacquired"),
    ]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    rows = metrics["reacquisition"]["by_gap"]
    assert metrics["robustness"]["occlusion_survival"]["wrong_target_associations"] == 2
    assert metrics["reacquisition"]["correct_target_rate"] == 0
    assert {(row["target_id"], row["swapped_with_target_id"]) for row in rows} == {
        ("left", "right"),
        ("right", "left"),
    }


def test_id_switch_count_is_exact() -> None:
    ground_truth = [gt(0), gt(100_000_000), gt(200_000_000)]
    records = [output(0, track="a"), output(100_000_000, track="b"), output(200_000_000, track="b")]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    assert metrics["identity"]["id_switches"] == 1
    assert metrics["identity"]["track_splits"] == 0


def test_duplicate_track_counts_only_extra_unmatched_track() -> None:
    ground_truth = [gt(0)]
    records = [output(0, track="primary"), output(0, track="duplicate", box=[11, 10, 21, 20])]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    assert metrics["identity"]["duplicate_tracks"] == 1
    assert metrics["identity"]["track_splits"] == 1


def test_background_hallucination_outside_ignore_is_false() -> None:
    target = gt(0, box=[10, 10, 20, 20])
    ignored = gt(0, target="ignore-object", box=[30, 30, 40, 40])
    ignored["ignore"] = True
    records = [
        output(0, track="target", box=[10, 10, 20, 20]),
        output(0, track="background-hallucination", box=[100, 100, 110, 110]),
    ]
    metrics, _ = calculate_metrics([target, ignored], records, Thresholds())
    assert metrics["false_detections"]["detections"] == 1
    assert metrics["false_detections"]["neutral_ignored_predictions"] == 0


def test_duplicate_target_prediction_is_duplicate_and_split_with_narrow_ignore() -> None:
    target = gt(0, box=[10, 10, 20, 20])
    ignored = gt(0, target="ignore-object", box=[30, 30, 40, 40])
    ignored["ignore"] = True
    records = [
        output(0, track="target", box=[10, 10, 20, 20]),
        output(0, track="duplicate-target", box=[11, 10, 21, 20]),
    ]
    metrics, _ = calculate_metrics([target, ignored], records, Thresholds())
    assert metrics["identity"]["duplicate_tracks"] == 1
    assert metrics["identity"]["track_splits"] == 1


def test_false_track_duration_is_exactly_two_seconds() -> None:
    ground_truth = [gt(0, box=[100, 100, 110, 110]), gt(1_000_000_000, box=[100, 100, 110, 110])]
    records = [output(0, track="false", box=[0, 0, 10, 10]), output(1_000_000_000, track="false", box=[0, 0, 10, 10])]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds(max_match_center_error_px=5))
    assert metrics["false_detections"]["track_duration_ms"]["median"] == 2000
    assert metrics["false_detections"]["longest_lived_false_track_ms"] == 2000
    assert metrics["false_detections"]["persistent_track_births"] == 1
    assert metrics["false_detections"]["one_frame_false_detections"] == 0


def test_external_latency_uses_collector_timestamp() -> None:
    metrics, _ = calculate_metrics(
        [gt(1_000_000_000)], [output(1_000_000_000, received_offset_ns=17_000_000)], Thresholds()
    )
    assert metrics["latency"]["median"] == 17
    assert metrics["latency"]["maximum"] == 17


def test_online_latency_uses_trusted_delivery_completion_not_native_source_time() -> None:
    source_timestamp_ns = 1_000_000_000
    delivered_timestamp_ns = 1_300_000_000
    record = output(
        source_timestamp_ns,
        received_offset_ns=305_000_000,
    )
    metrics, _ = calculate_metrics(
        [gt(source_timestamp_ns)],
        [record],
        Thresholds(latency_deadline_ms=50),
        frame_delivery_ns={("seq", source_timestamp_ns): delivered_timestamp_ns},
    )

    assert metrics["latency"]["median"] == 5
    assert metrics["latency"]["deadline_miss_rate"] == 0
    assert metrics["latency"]["native_source_offset_ms"]["median"] == 305


def test_online_latency_ignores_output_without_a_successful_delivery_boundary() -> None:
    source_timestamp_ns = 1_000_000_000
    metrics, _ = calculate_metrics(
        [gt(source_timestamp_ns)],
        [output(source_timestamp_ns, received_offset_ns=5_000_000)],
        Thresholds(),
        frame_delivery_ns={},
    )

    assert metrics["latency"]["sample_count"] == 0
    assert metrics["latency"]["native_source_offset_ms"]["sample_count"] == 0


def test_outputs_received_before_source_time_never_create_negative_latency() -> None:
    records = [
        output(1_000_000_000, state="tentative", received_offset_ns=-10_000_000),
        output(1_100_000_000, state="confirmed", received_offset_ns=-5_000_000),
        output(1_200_000_000, state="confirmed", received_offset_ns=7_000_000),
    ]
    metrics, _ = calculate_metrics(
        [gt(1_000_000_000), gt(1_100_000_000), gt(1_200_000_000)], records, Thresholds()
    )
    assert metrics["latency"]["sample_count"] == 1
    assert metrics["latency"]["first_tentative_ms"] is None
    assert metrics["latency"]["first_confirmed_ms"] == 7
    assert metrics["latency"]["by_target_count"]["1"]["sample_count"] == 1
    assert all(value >= 0 for value in metrics["latency"]["over_time_ms"])


def test_acceptance_fixture_exact_220_ms_acquisition_and_350_ms_dropout() -> None:
    timestamps = [
        1_000_000_000,
        1_220_000_000,
        3_900_000_000,
        4_000_000_000,
        4_100_000_000,
        4_200_000_000,
        4_300_000_000,
        4_350_000_000,
    ]
    ground_truth = [gt(timestamp) for timestamp in timestamps]
    records = [output(1_220_000_000), output(3_900_000_000), output(4_350_000_000)]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds(visible_dropout_tolerance_ms=0))
    assert metrics["acquisition"]["median"] == 220
    assert metrics["visible_dropouts"]["maximum"] == 350


def test_predicted_output_never_counts_as_reacquisition() -> None:
    ground_truth = [
        gt(0),
        gt(100_000_000, eligible=False, visibility=0, occlusion="full"),
        gt(200_000_000),
    ]
    records = [
        output(0),
        output(100_000_000, support="predicted", state="coasting"),
        output(200_000_000, support="predicted", state="coasting"),
    ]
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    assert metrics["reacquisition"]["correct_target_rate"] == 0
    assert metrics["coverage"]["overall_observed"] < metrics["coverage"]["overall_continuity"]


def test_freshness_requires_exact_source_timestamp() -> None:
    metrics, _ = calculate_metrics([gt(100)], [output(101)], Thresholds())
    assert metrics["sample_counts"]["matches"] == 0


def test_unknown_visibility_does_not_invent_score_strata() -> None:
    row = gt(0)
    row["visibility_fraction"] = None
    row["occlusion"] = "unknown"
    metrics, _ = calculate_metrics([row], [output(0)], Thresholds())
    assert metrics["coverage"]["by_visibility"] == {}
    assert metrics["localization"]["mean_iou_by_visibility"] == {}


def test_ignore_annotations_neutralize_unmatched_predictions_after_target_matching() -> None:
    target = gt(0, box=[0, 0, 10, 10])
    ignored = gt(0, target="ignore-1", box=[20, 20, 40, 40])
    ignored["ignore"] = True
    records = [output(0, box=[0, 0, 10, 10]), output(0, track="unlabeled", box=[20, 20, 40, 40])]
    metrics, _ = calculate_metrics(
        [target, ignored], records, Thresholds(ignore_match_iou=0.5, max_match_center_error_px=30)
    )
    assert metrics["sample_counts"]["matches"] == 1
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0
    assert metrics["false_detections"]["neutral_ignored_predictions"] == 1


def test_ordinary_ignore_accepts_the_exact_iou_boundary() -> None:
    ignored = gt(0, target="ignore-1", box=[0, 0, 20, 10])
    ignored["ignore"] = True
    prediction = output(0, track="boundary", box=[0, 0, 10, 10])
    metrics, _ = calculate_metrics([ignored], [prediction], Thresholds(ignore_match_iou=0.5))
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0


@pytest.mark.parametrize(
    ("ignore_region", "class_agnostic"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_target_compatible_duplicate_survives_overlapping_ignore(
    ignore_region: bool, class_agnostic: bool
) -> None:
    target = gt(0, box=[0, 0, 10, 10])
    ignored = gt(0, target="ignore-object", box=[20, 0, 30, 10])
    ignored["ignore"] = True
    ignored["ignore_region"] = ignore_region
    ignored["class_id"] = "car" if class_agnostic else "person"
    primary = output(0, track="primary", box=[0, 0, 10, 10])
    duplicate = output(0, track="duplicate", box=[20, 0, 30, 10])
    metrics, _ = calculate_metrics(
        [target, ignored],
        [primary, duplicate],
        Thresholds(class_agnostic=class_agnostic, max_match_center_error_px=30),
    )
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 0
    assert metrics["false_detections"]["detections"] == 1
    assert metrics["false_detections"]["track_births"] == 1
    assert metrics["identity"]["duplicate_tracks"] == 1
    assert metrics["identity"]["track_splits"] == 1


def test_legitimate_non_target_ignore_still_neutralizes() -> None:
    target = gt(0, box=[0, 0, 10, 10])
    ignored = gt(0, target="ignore-object", box=[40, 40, 60, 60])
    ignored["ignore"] = True
    prediction = output(0, track="non-target", box=[40, 40, 60, 60])
    metrics, _ = calculate_metrics([target, ignored], [output(0, track="primary"), prediction], Thresholds())
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0


def test_class_aware_wrong_class_ordinary_ignore_is_false_and_births_a_track() -> None:
    ignored_car = gt(0, target="ignore-car", box=[20, 20, 40, 40])
    ignored_car["ignore"] = True
    ignored_car["class_id"] = "car"
    prediction = output(0, track="person-over-car-ignore", box=[20, 20, 40, 40])
    metrics, _ = calculate_metrics([ignored_car], [prediction], Thresholds(ignore_match_iou=0.5))
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 0
    assert metrics["false_detections"]["detections"] == 1
    assert metrics["false_detections"]["track_births"] == 1


def test_class_aware_same_class_ordinary_ignore_is_neutral() -> None:
    ignored_person = gt(0, target="ignore-person", box=[20, 20, 40, 40])
    ignored_person["ignore"] = True
    metrics, _ = calculate_metrics(
        [ignored_person], [output(0, track="person-over-person-ignore", box=[20, 20, 40, 40])], Thresholds()
    )
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0


def test_class_agnostic_wrong_class_ordinary_ignore_is_neutral() -> None:
    ignored_car = gt(0, target="ignore-car", box=[20, 20, 40, 40])
    ignored_car["ignore"] = True
    ignored_car["class_id"] = "car"
    metrics, _ = calculate_metrics(
        [ignored_car],
        [output(0, track="person-over-car-ignore", box=[20, 20, 40, 40])],
        Thresholds(class_agnostic=True),
    )
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0


def test_class_aware_ignore_region_requires_a_compatible_class() -> None:
    ignored_car = gt(0, target="ignore-car-region", box=[0, 0, 100, 100])
    ignored_car["ignore"] = True
    ignored_car["ignore_region"] = True
    ignored_car["class_id"] = "car"
    wrong_class = output(0, track="person-in-car-region", box=[10, 10, 20, 20])
    wrong_metrics, _ = calculate_metrics([ignored_car], [wrong_class], Thresholds())
    assert wrong_metrics["sample_counts"]["neutral_ignored_predictions"] == 0
    assert wrong_metrics["false_detections"]["detections"] == 1
    same_class = output(0, track="car-in-car-region", box=[10, 10, 20, 20])
    same_class.system_record["class_id"] = "car"
    same_metrics, _ = calculate_metrics([ignored_car], [same_class], Thresholds())
    assert same_metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert same_metrics["false_detections"]["detections"] == 0
    agnostic_metrics, _ = calculate_metrics(
        [ignored_car], [wrong_class], Thresholds(class_agnostic=True)
    )
    assert agnostic_metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert agnostic_metrics["false_detections"]["detections"] == 0


def test_ignored_ground_truth_rows_are_not_in_multi_target_denominator() -> None:
    target = gt(0, target="target", box=[0, 0, 10, 10])
    ignored = gt(0, target="unlabeled", box=[20, 20, 40, 40])
    ignored["ignore"] = True
    metrics, _ = calculate_metrics(
        [target, ignored], [output(0, track="target", box=[0, 0, 10, 10])], Thresholds()
    )
    assert metrics["multi_target"] == {"1": {"matched": 1, "eligible": 1, "coverage": 1.0}}


def test_contained_small_prediction_is_neutral_inside_broad_ignore_region() -> None:
    ignored = gt(0, target="ignore-region", box=[0, 0, 100, 100])
    ignored["ignore"] = True
    ignored["ignore_region"] = True
    metrics, _ = calculate_metrics(
        [ignored], [output(0, track="unlabeled", box=[10, 10, 20, 20])], Thresholds()
    )
    assert intersection_over_prediction_area([10, 10, 20, 20], [0, 0, 100, 100]) == 1
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["false_detections"]["detections"] == 0


def test_neutral_predictions_do_not_create_identity_penalties() -> None:
    target = gt(0, box=[10, 10, 20, 20])
    ignored = gt(0, target="ignore-region", box=[0, 0, 100, 100])
    ignored["ignore"] = True
    ignored["ignore_region"] = True
    records = [
        output(0, track="real", box=[10, 10, 20, 20]),
        output(0, track="neutral", box=[50, 50, 60, 60]),
        output(100_000_000, track="real", box=[10, 10, 20, 20]),
    ]
    metrics, _ = calculate_metrics([target, ignored, gt(100_000_000)], records, Thresholds())
    assert metrics["sample_counts"]["neutral_ignored_predictions"] == 1
    assert metrics["identity"]["duplicate_tracks"] == 0
    assert metrics["identity"]["track_splits"] == 0
    assert metrics["identity"]["id_switches"] == 0
    assert metrics["long_running_stability"]["unique_track_ids"] == 1


def test_eof_uses_median_cadence_and_half_open_intervals() -> None:
    ground_truth = [gt(0), gt(100_000_000), gt(200_000_000)]
    metrics, _ = calculate_metrics(ground_truth, [output(0), output(100_000_000)], Thresholds())
    assert metrics["coverage"]["eligible_target_time_ms"] == 300
    assert metrics["coverage"]["overall_observed"] == 2 / 3


def test_percentiles_use_linear_interpolation() -> None:
    assert percentile([0, 100], 0.95) == 95


def test_feed_interruption_recovery_reports_observed_same_id() -> None:
    ground_truth = [gt(0), gt(100_000_000), gt(200_000_000)]
    records = [output(0, track="same"), output(100_000_000, track="same"), output(200_000_000, track="same")]
    metrics, _ = calculate_metrics(
        ground_truth,
        records,
        Thresholds(),
        fault_timestamps={("seq", 100_000_000)},
        fault_events={("seq", 100_000_000): ["feed_interruption"]},
    )
    result = metrics["robustness"]["feed_faults"]["feed_interruption"]
    assert result["observed_recovery_rate"] == 1
    assert result["same_id_recovery_rate"] == 1
    assert metrics["reacquisition"]["after_feed_interruption_rate"] == 1
