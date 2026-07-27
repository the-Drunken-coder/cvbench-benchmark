from cvbench.stability import evaluate_long_run_assertions


def test_all_long_run_release_assertions_are_explicit_and_passed() -> None:
    observations = {
        "unique_track_ids": 42,
        "distinct_physical_target_births": 42,
        "track_id_reuse_events": 0,
        "track_id_exhaustion_detected": False,
        "state_contamination_events": 0,
        "false_positive_accumulation_per_camera_minute": -0.5,
        "interruption_recovery_rate": 1.0,
        "latency_drift_ms": -12.0,
    }
    declared = {
        "max_unique_track_ids": 100,
        "min_distinct_physical_target_births": 40,
        "max_track_id_reuse_events": 0,
        "require_no_track_id_exhaustion": True,
        "max_state_contamination_events": 0,
        "max_false_positive_accumulation_per_camera_minute": 0,
        "min_interruption_recovery_rate": 1.0,
        "max_absolute_latency_drift_ms": 20,
        "max_memory_growth_bytes": 1_000_000,
    }
    result = evaluate_long_run_assertions(observations, {"memory_growth_bytes": 500_000}, declared)
    assert result["passed"] is True
    assert set(result["assertions"]) == set(declared)


def test_missing_soak_evidence_is_explicitly_unavailable() -> None:
    result = evaluate_long_run_assertions(
        {"interruption_recovery_rate": None},
        {"memory_growth_bytes": None},
        {"min_interruption_recovery_rate": 1.0, "max_memory_growth_bytes": 10},
    )
    assert result["passed"] is None
    assert result["assertions_evaluated"] == 0
    assert result["assertions_unavailable"] == ["max_memory_growth_bytes", "min_interruption_recovery_rate"]
    assert all(check["evaluated"] is False for check in result["assertions"].values())
    assert all(check["passed"] is None for check in result["assertions"].values())


def test_measured_assertion_still_fails_normally() -> None:
    result = evaluate_long_run_assertions(
        {"latency_drift_ms": 11.0},
        {"memory_growth_bytes": None},
        {"max_absolute_latency_drift_ms": 10.0},
    )
    assert result["passed"] is False
    assert result["assertions"]["max_absolute_latency_drift_ms"]["evaluated"] is True
    assert result["assertions"]["max_absolute_latency_drift_ms"]["passed"] is False
