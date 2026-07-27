from cvbench.diagnostics import generate_findings


def test_low_tracking_quality_and_id_switches_are_major_findings() -> None:
    metrics = {
        "acquisition": {"rate": 0.2},
        "coverage": {"overall_observed": 0.1},
        "visible_dropouts": {"count": 0},
        "identity": {"id_switches": 3},
        "long_running_stability": {
            "track_id_reuse_events": 1,
            "ended_track_id_reuse_events": 1,
            "active_track_id_alias_events": 0,
            "track_id_reuse_evidence": [{"track_id": "wrapped-0"}],
        },
        "false_detections": {"track_births": 0},
        "reacquisition": {"events": 0},
        "latency": {"deadline_miss_rate": 0},
    }
    findings = generate_findings(metrics, {}, {}, [])
    identifiers = {finding["finding_id"] for finding in findings}
    assert {"TRACK-QUALITY-001", "IDENTITY-SWITCH-001", "TRACK-ID-REUSE-001"} <= identifiers
    assert all(finding["severity"] == "high" for finding in findings)
