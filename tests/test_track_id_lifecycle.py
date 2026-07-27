from cvbench.config import Thresholds
from cvbench.diagnostics import generate_findings
from cvbench.metrics import calculate_metrics
from cvbench.stability import evaluate_long_run_assertions
from tests.helpers import gt, output


def _counter_fixture(modulus: int | None) -> tuple[list[dict], list]:
    ground_truth = []
    records = []
    for birth in range(40):
        timestamp = birth * 2_000_000
        track_number = birth if modulus is None else birth % modulus
        track_id = f"counter-{track_number}"
        ground_truth.append(gt(timestamp, target=f"physical-{birth}"))
        started = output(timestamp, track=track_id)
        started.system_record["event"] = "track_started"
        records.append(started)
        ended = output(timestamp + 1_000_000, track=track_id, support="predicted", state="lost")
        ended.system_record["event"] = "track_ended"
        records.append(ended)
    return ground_truth, records


def test_32_id_counter_wrap_is_detected_across_40_distinct_births() -> None:
    ground_truth, records = _counter_fixture(32)
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    lifecycle = metrics["long_running_stability"]
    evaluated = evaluate_long_run_assertions(
        lifecycle,
        {"memory_growth_bytes": 0},
        {"require_no_track_id_exhaustion": True},
    )
    assert lifecycle["unique_track_ids"] == 32
    assert lifecycle["distinct_physical_target_births"] == 40
    assert lifecycle["track_id_reuse_events"] == 8
    assert lifecycle["ended_track_id_reuse_events"] == 8
    assert lifecycle["active_track_id_alias_events"] == 0
    assert lifecycle["track_id_reuse_evidence"][0] == {
        "kind": "reuse_after_terminal",
        "sequence_id": "seq",
        "track_id": "counter-0",
        "previous_target_id": "physical-0",
        "new_target_id": "physical-32",
        "previous_assignment_timestamp_ns": 0,
        "reuse_timestamp_ns": 64_000_000,
    }
    assert lifecycle["track_id_exhaustion_detected"] is True
    assert evaluated["assertions"]["require_no_track_id_exhaustion"]["passed"] is False
    assert evaluated["passed"] is False
    findings = generate_findings(metrics, {}, {}, [])
    assert any(finding["finding_id"] == "TRACK-ID-REUSE-001" for finding in findings)


def test_non_wrapping_counter_passes_same_40_birth_control() -> None:
    ground_truth, records = _counter_fixture(None)
    metrics, _ = calculate_metrics(ground_truth, records, Thresholds())
    lifecycle = metrics["long_running_stability"]
    evaluated = evaluate_long_run_assertions(
        lifecycle,
        {"memory_growth_bytes": 0},
        {"require_no_track_id_exhaustion": True},
    )
    assert lifecycle["unique_track_ids"] == 40
    assert lifecycle["distinct_physical_target_births"] == 40
    assert lifecycle["track_id_reuse_events"] == 0
    assert lifecycle["track_id_exhaustion_detected"] is False
    assert evaluated["passed"] is True


def test_active_alias_is_detected_but_same_target_reacquisition_is_legitimate() -> None:
    alias_metrics, _ = calculate_metrics(
        [gt(0, target="left", box=[0, 0, 10, 10]), gt(0, target="right", box=[100, 0, 110, 10])],
        [output(0, track="shared", box=[0, 0, 10, 10]), output(0, track="shared", box=[100, 0, 110, 10])],
        Thresholds(),
    )
    assert alias_metrics["long_running_stability"]["active_track_id_alias_events"] == 1

    ended = output(1_000_000, track="stable", support="predicted", state="lost")
    ended.system_record["event"] = "track_ended"
    reacquired_metrics, _ = calculate_metrics(
        [gt(0), gt(2_000_000)],
        [output(0, track="stable"), ended, output(2_000_000, track="stable", state="reacquired")],
        Thresholds(),
    )
    assert reacquired_metrics["long_running_stability"]["track_id_reuse_events"] == 0
