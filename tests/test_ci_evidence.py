import json
import sys
from pathlib import Path

import pytest

from scripts.assert_docker_report import _parse_mode
from scripts.assert_docker_report import main as assert_docker_report
from scripts.evidence_hashes import main as evidence_hashes
from scripts.sanitize_ci_report import sanitize_runs
from scripts.verify_ci_evidence import _assert_safe, main
from tests.test_replay_pacing import _run


def _generated_run(tmp_path: Path) -> tuple[Path, Path]:
    _run(tmp_path, "accelerated-test-20x")
    runs = tmp_path / "runs-accelerated-test-20x-online_replay"
    reports = list(runs.glob("*/report.json"))
    assert len(reports) == 1
    return runs, reports[0]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["assert_docker_report.py", "runs"], "synthetic"),
        (["assert_docker_report.py", "runs", "--real-video"], "real-video"),
        (["assert_docker_report.py", "runs", "--combined"], "combined"),
    ],
)
def test_docker_report_mode_flags_reach_their_named_contract(argv: list[str], expected: str) -> None:
    assert _parse_mode(argv) == expected


def test_combined_report_rejects_duplicate_scenario_even_when_set_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_ids = [
        "synthetic-acquisition",
        "synthetic-false-detection",
        "synthetic-multi-target-identity",
        "synthetic-multi-target-pair",
        "synthetic-occlusion-gap-1000ms",
        "synthetic-occlusion-gap-100ms",
        "synthetic-occlusion-gap-2000ms",
        "synthetic-occlusion-gap-250ms",
        "synthetic-occlusion-gap-500ms",
        "synthetic-occlusion-reacquisition",
        "synthetic-resource-stress",
        "synthetic-track-id-churn",
        "synthetic-visible-retention",
        "rvmot-a1c9",
        "rvmot-b7e2",
        "rvmot-c4f6",
    ]
    run = tmp_path / "runs" / "one"
    run.mkdir(parents=True)
    report = {
        "outcome": {"status": "completed"},
        "benchmark": {"id": "public-whole-system-tracking", "version": "2.0.0"},
        "metrics": {
            "sample_counts": {"matches": 1},
            "multi_object_tracking": {"hota": 0},
        },
        "provenance": {
            "comparison_inputs": {
                "scenarios": [{"id": scenario_id} for scenario_id in [*scenario_ids, scenario_ids[0]]]
            }
        },
        "runtime_isolation": {},
    }
    (run / "report.json").write_text(json.dumps(report))
    monkeypatch.setattr("sys.argv", ["assert_docker_report.py", str(tmp_path / "runs"), "--combined"])
    with pytest.raises(AssertionError):
        assert_docker_report()


def test_safe_report_and_resources_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs, _report = _generated_run(tmp_path)
    safe_run = sanitize_runs(runs, tmp_path / "safe")
    manifest = tmp_path / "artifacts.sha256"
    manifest.write_text("a" * 64 + "  frame.jpg\n")
    monkeypatch.setattr("sys.argv", ["verify_ci_evidence.py", str(safe_run.parent), str(manifest)])
    main()


def test_evidence_hash_manifest_binds_exact_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "report.json").write_text('{"outcome":"completed"}\n')
    (tmp_path / "resources.csv").write_text("elapsed_ms\n100\n")
    monkeypatch.setattr(sys, "argv", ["evidence_hashes.py", "evidence.sha256", "report.json", "resources.csv"])
    assert evidence_hashes() == 0
    monkeypatch.setattr(sys, "argv", ["evidence_hashes.py", "evidence.sha256", "--verify"])
    assert evidence_hashes() == 0
    (tmp_path / "report.json").write_text("changed")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        evidence_hashes()


def test_restricted_ground_truth_payload_is_rejected(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"bbox_xyxy": [1, 2, 3, 4]}')
    with pytest.raises(AssertionError):
        _assert_safe(report)


def test_ci_sanitization_writes_a_safe_copy_without_mutating_core_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, source_report = _generated_run(tmp_path)
    core = json.loads(source_report.read_text())
    core["audit_evidence"]["frame_samples"] = [
        {"ground_truth": [{"bbox_xyxy": [1, 2, 3, 4]}], "predictions": [], "matches": []}
    ]
    core["diagnostics"]["sut_stderr"] = ["secret-model-output"]
    source_report.write_text(json.dumps(core))
    original = source_report.read_text()

    destination_run = sanitize_runs(source, tmp_path / "safe")
    assert source_report.read_text() == original
    safe = json.loads((destination_run / "report.json").read_text())
    assert safe["schema_version"] == "cvbench.report-redacted/v1"
    assert safe["source_schema_version"] == "cvbench.report/v1"
    assert safe["redaction"]["schema_version"] == "cvbench.redaction/v1"
    assert safe["audit_evidence"]["redacted"] is True
    assert safe["diagnostics"]["schema_version"] == "cvbench.diagnostics-redacted/v1"
    manifest = tmp_path / "artifacts.sha256"
    manifest.write_text("a" * 64 + "  frame.jpg\n")
    monkeypatch.setattr(
        "sys.argv",
        ["verify_ci_evidence.py", str(destination_run.parent), str(manifest)],
    )
    main()
    _assert_safe(destination_run / "report.json")


def test_failed_isolation_remains_unknown_in_public_safe_copy(tmp_path: Path) -> None:
    source, source_report = _generated_run(tmp_path)
    core = json.loads(source_report.read_text())
    core["outcome"] = {
        "status": "failed",
        "exit_code": 1,
        "startup_time_ms": None,
        "time_to_first_output_ms": None,
        "errors": ["container ID was not created"],
        "resolved_image": None,
        "timed_out": False,
        "crashed": True,
    }
    core["runtime_isolation"].update({
        "status": "verification_failed",
        "future_frame_isolation": None,
        "ground_truth_access": None,
        "repository_access": None,
        "media_access": None,
        "mounts": None,
        "network_mode": None,
        "image_identity_verified": None,
        "container_user_alignment_verified": None,
    })
    source_report.write_text(json.dumps(core))

    destination = sanitize_runs(source, tmp_path / "safe")
    safe = json.loads((destination / "report.json").read_text())
    isolation = safe["runtime_isolation"]
    assert safe["outcome"]["status"] == "failed"
    assert isolation["status"] == "verification_failed"
    assert isolation["future_frame_isolation"] is None
    assert isolation["ground_truth_access"] is None
    assert isolation["repository_access"] is None
    assert isolation["media_access"] is None
    assert isolation["image_identity_verified"] is None


def test_safe_artifact_rejects_core_or_redaction_schema_spoofing(tmp_path: Path) -> None:
    source, _report = _generated_run(tmp_path)
    destination = sanitize_runs(source, tmp_path / "safe")
    safe_report = destination / "report.json"
    safe = json.loads(safe_report.read_text())
    safe["schema_version"] = "cvbench.report/v1"
    safe_report.write_text(json.dumps(safe))
    with pytest.raises(ValueError, match="cvbench.report-redacted/v1"):
        _assert_safe(safe_report)
