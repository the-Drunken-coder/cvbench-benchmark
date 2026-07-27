from __future__ import annotations

from typing import Any


def _finding(
    identifier: str,
    category: str,
    severity: str,
    observation: dict[str, Any],
    statement: str,
    evidence: list[str],
    causes: list[str],
    test: str,
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "finding_id": identifier,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "status": "confirmed",
        "observation": observation,
        "interpretation": {"statement": statement, "evidence": evidence},
        "possible_causes": causes,
        "recommended_test": test,
    }


def generate_findings(
    metrics: dict[str, Any], outcome: dict[str, Any], resources: dict[str, Any], collector_errors: list[str]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if outcome.get("timed_out"):
        findings.append(
            _finding(
                "RUN-TIMEOUT-001",
                "runtime",
                "critical",
                {"timed_out": True, "exit_code": outcome.get("exit_code")},
                "The system did not complete within the configured deadline.",
                ["The runner's external deadline expired."],
                ["The system may be hung.", "Shutdown or frame consumption may be blocked."],
                "minimal-sequence-timeout-reproduction",
            )
        )
    if outcome.get("crashed"):
        findings.append(
            _finding(
                "RUN-CRASH-001",
                "runtime",
                "critical",
                {"exit_code": outcome.get("exit_code"), "errors": outcome.get("errors", [])},
                "The system process exited unsuccessfully during the run.",
                ["The externally observed process exit code was non-zero."],
                ["An unhandled process error occurred."],
                "repeat-with-same-run-configuration",
            )
        )
    flood_errors = [error for error in collector_errors if error.startswith("output limit exceeded:")]
    invalid_errors = [error for error in collector_errors if error not in flood_errors]
    if flood_errors:
        findings.append(
            _finding(
                "OUTPUT-FLOOD-001",
                "protocol",
                "critical",
                {"limits_exceeded": flood_errors},
                "The SUT exceeded a bounded stdout safety limit.",
                ["The collector enforced byte and record limits before JSON decoding."],
                ["The system may be emitting oversized or unbounded output."],
                "bounded-output-flood-reproduction",
            )
        )
    if invalid_errors:
        findings.append(
            _finding(
                "OUTPUT-INVALID-001",
                "protocol",
                "high",
                {"invalid_record_count": len(invalid_errors), "examples": invalid_errors[:3]},
                "The SUT emitted records that failed deterministic schema validation.",
                ["The output collector rejected each listed record."],
                ["The system output schema may not match cvbench.track/v1."],
                "output-schema-validation",
            )
        )
    dropouts = metrics.get("visible_dropouts", {})
    if dropouts.get("count", 0):
        findings.append(
            _finding(
                "VIS-DROPOUT-001",
                "tracking_continuity",
                "high",
                {
                    "visible_target": True,
                    "dropout_count": dropouts["count"],
                    "longest_dropout_ms": dropouts.get("maximum"),
                    "latency_p99_ms": metrics.get("latency", {}).get("p99"),
                    "peak_cpu_percent": resources.get("peak_cpu_percent"),
                },
                "Eligible visible targets had observed-output gaps beyond the configured tolerance.",
                ["Ground truth remained visible and eligible.", "No correct observed match existed during each gap."],
                ["Frame processing may be falling behind.", "Detection or association may be intermittent."],
                "cpu-and-input-rate-sweep",
            )
        )
    acquisition_rate = metrics.get("acquisition", {}).get("rate")
    observed_coverage = metrics.get("coverage", {}).get("overall_observed")
    if (acquisition_rate is not None and acquisition_rate < 0.9) or (
        observed_coverage is not None and observed_coverage < 0.8
    ):
        findings.append(
            _finding(
                "TRACK-QUALITY-001",
                "tracking_accuracy",
                "high",
                {"acquisition_rate": acquisition_rate, "observed_coverage": observed_coverage},
                "The system missed a substantial portion of eligible target observations.",
                ["Acquisition and coverage were calculated from fresh deterministic matches."],
                ["Detection may be unreliable.", "Localization or class output may fail matching gates."],
                "scenario-family-accuracy-breakdown",
            )
        )
    id_switches = metrics.get("identity", {}).get("id_switches", 0)
    if id_switches:
        findings.append(
            _finding(
                "IDENTITY-SWITCH-001",
                "identity_integrity",
                "high",
                {"id_switches": id_switches},
                "Matched physical targets changed output identity during the run.",
                ["The deterministic match history contains an output track-ID transition."],
                ["Association state may be unstable.", "Track IDs may be recreated too aggressively."],
                "crossing-and-occlusion-identity-sweep",
            )
        )
    lifecycle = metrics.get("long_running_stability", {})
    if lifecycle.get("track_id_reuse_events", 0):
        findings.append(
            _finding(
                "TRACK-ID-REUSE-001",
                "identity_integrity",
                "high",
                {
                    "track_id_reuse_events": lifecycle["track_id_reuse_events"],
                    "ended_track_id_reuse_events": lifecycle.get("ended_track_id_reuse_events", 0),
                    "active_track_id_alias_events": lifecycle.get("active_track_id_alias_events", 0),
                    "examples": lifecycle.get("track_id_reuse_evidence", [])[:3],
                },
                "A track ID was assigned to more than one physical target within a sequence.",
                ["Deterministic observed-match lifecycle evidence identifies each reassignment."],
                ["The track-ID counter may have wrapped or association state may be aliasing active targets."],
                "track-id-lifecycle-churn-reproduction",
            )
        )
    false_tracks = metrics.get("false_detections", {}).get("track_births", 0)
    if false_tracks:
        findings.append(
            _finding(
                "FALSE-TRACK-001",
                "false_detection",
                "medium",
                {
                    "false_track_births": false_tracks,
                    "longest_false_track_ms": metrics["false_detections"].get("longest_lived_false_track_ms"),
                },
                "One or more output tracks could not be associated with an on-screen target.",
                ["Deterministic gated matching left the track records unmatched."],
                ["A distractor may be classified as a target.", "Track termination may be too permissive."],
                "empty-and-distractor-sequence",
            )
        )
    reacquisition = metrics.get("reacquisition", {})
    if reacquisition.get("events") and reacquisition.get("correct_target_rate", 1) < 1:
        findings.append(
            _finding(
                "REACQ-MISS-001",
                "reacquisition",
                "high",
                {"events": reacquisition["events"], "correct_target_rate": reacquisition["correct_target_rate"]},
                "At least one target was not correctly reacquired after a controlled vision-loss interval.",
                ["No correct observed match followed the target's return."],
                ["Track state may expire too early.", "The detector may not recover promptly."],
                "occlusion-duration-sweep",
            )
        )
    latency = metrics.get("latency", {})
    if latency.get("deadline_miss_rate") and latency["deadline_miss_rate"] > 0:
        findings.append(
            _finding(
                "LAT-DEADLINE-001",
                "latency",
                "medium",
                {"deadline_ms": latency.get("deadline_ms"), "miss_rate": latency["deadline_miss_rate"]},
                "Externally received track updates missed the configured latency deadline.",
                ["Collector timestamps were later than the source deadline."],
                ["Processing time or queueing may exceed the available frame budget."],
                "input-rate-and-latency-sweep",
            )
        )
    return findings
