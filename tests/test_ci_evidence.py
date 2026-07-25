import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from scripts.assert_docker_report import _parse_mode
from scripts.assert_docker_report import main as assert_docker_report
from scripts.evidence_hashes import main as evidence_hashes
from scripts.sanitize_ci_report import sanitize_runs
from scripts.verify_ci_evidence import _assert_safe, main
from scripts.verify_committed_mot_evidence import (
    EVIDENCE_FILES,
    verify_corpus_manifests,
    verify_hash_bound_files,
)
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
        (["assert_docker_report.py", "runs", "--motchallenge"], "motchallenge"),
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
        "mot17-02",
        "mot17-04",
        "mot17-09",
        "mot17-10",
        "mot17-11",
        "mot17-13",
        "mot20-01",
        "mot20-02",
        "mot20-03",
        "mot20-05",
    ]
    run = tmp_path / "runs" / "one"
    run.mkdir(parents=True)
    report = {
        "outcome": {"status": "completed"},
        "benchmark": {"id": "public-whole-system-tracking", "version": "3.0.0"},
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


def test_safe_evidence_accepts_and_validates_multiple_corpus_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, _report = _generated_run(tmp_path)
    safe_run = sanitize_runs(runs, tmp_path / "safe")
    first = tmp_path / "first.sha256"
    second = tmp_path / "second.sha256"
    first.write_text("a" * 64 + "  first/frame.jpg\n")
    second.write_text("b" * 64 + "  second/frame.jpg\n")
    monkeypatch.setattr(
        "sys.argv",
        ["verify_ci_evidence.py", str(safe_run.parent), str(first), str(second)],
    )
    main()
    second.write_text("not-a-hash  second/frame.jpg\n")
    with pytest.raises(AssertionError):
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


def test_pr_docker_ci_is_hermetic_and_requires_full_mot_evidence_verification() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_text = (root / ".github/workflows/ci.yml").read_text()
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["docker-scored-e2e"]["steps"]
    run_contract = "\n".join(step.get("run", "") for step in steps)
    upload_contract = "\n".join(
        str(step.get("with", {}).get("path", ""))
        for step in steps
        if step.get("uses") == "actions/upload-artifact@v4"
    )

    assert "scripts/fresh_checkout_runner_e2e.py" in run_contract
    assert "benchmarks/real-video-v2.yaml" in run_contract
    assert "scripts/verify_committed_mot_evidence.py" in run_contract
    assert "evidence/motchallenge-v1/corpus-manifests.json" in run_contract
    assert "scenarios/motchallenge-v1/expected-frame-sha256.txt" in run_contract
    assert "scenarios/motchallenge-v1/normalized-ground-truth-sha256.txt" in run_contract
    assert "prepare_motchallenge.py" not in run_contract
    assert "requirements-motchallenge.lock" not in run_contract
    assert "--allow-official-download" not in workflow_text
    assert "motchallenge.net" not in workflow_text
    assert set(upload_contract.split()) >= EVIDENCE_FILES


def test_trusted_full_scoring_command_requires_local_official_archives() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/run_trusted_mot_evidence.sh").read_text()
    assert all(name in script for name in ("MOT16.zip", "MOT17Labels.zip", "MOT20.zip"))
    assert "benchmarks/motchallenge-v1.yaml" in script
    assert "benchmarks/public-whole-system-v3.yaml" in script
    assert "data/motchallenge-v1/artifacts.sha256" in script
    assert "--allow-official-download" not in script
    assert "motchallenge.net" not in script
    assert "pip install" not in script
    assert "python -m pip" not in script
    assert "--index-url" not in script
    assert "docker build" not in script
    assert "scripts/trusted_mot_environment.py" in script
    assert "python3 -m cvbench.cli run" in script
    assert 'export PYTHONPATH="$repo_root/src"' in script
    environment_check = (root / "scripts/trusted_mot_environment.py").read_text()
    assert all(
        lock_file in environment_check
        for lock_file in ("requirements-real-video.lock", "requirements-motchallenge.lock")
    )


def test_committed_evidence_manifest_cannot_silently_omit_a_required_file(tmp_path: Path) -> None:
    required = {"one/report.json", "two/resources.csv"}
    for relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    manifest = tmp_path / "artifacts.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()}  {relative}\n"
            for relative in sorted(required)
        )
    )
    verify_hash_bound_files(tmp_path, manifest, required)
    with pytest.raises(RuntimeError, match="omits or adds"):
        verify_hash_bound_files(tmp_path, manifest, required | {"missing.json"})


def test_both_corpus_manifest_fingerprints_are_required_and_fail_on_drift(tmp_path: Path) -> None:
    frame_manifest = tmp_path / "scenarios/motchallenge-v1/expected-frame-sha256.txt"
    truth_manifest = tmp_path / "scenarios/motchallenge-v1/normalized-ground-truth-sha256.txt"
    frame_manifest.parent.mkdir(parents=True)
    frame_manifest.write_text(f"{'a' * 64}  mot17-02/frames/frame-000001.jpg\n")
    truth_manifest.write_text(f"{'b' * 64}  mot17-02/ground_truth.jsonl\n")
    real_manifest = tmp_path / "real-video-artifacts.sha256"
    real_manifest.write_text(f"{'c' * 64}  rvmot-a1c9/frames/frame-000000.jpg\n")
    mot_canonical = (
        f"{'a' * 64}  mot17-02/frames/frame-000001.jpg\n"
        f"{'b' * 64}  mot17-02/ground_truth.jsonl\n"
    ).encode()
    fingerprints = tmp_path / "evidence/motchallenge-v1/corpus-manifests.json"
    fingerprints.parent.mkdir(parents=True)
    fingerprints.write_text(
        json.dumps(
            {
                "schema_version": "cvbench.corpus-manifest-fingerprints/v1",
                "corpora": {
                    "motchallenge-v1": {
                        "artifact_manifest_entries": 2,
                        "artifact_manifest_path": (
                            "evidence/motchallenge-v1/corpora/"
                            "motchallenge-v1-artifacts.sha256"
                        ),
                        "artifact_manifest_sha256": hashlib.sha256(mot_canonical).hexdigest(),
                    },
                    "real-video-v2": {
                        "artifact_manifest_entries": 1,
                        "artifact_manifest_path": (
                            "evidence/motchallenge-v1/corpora/"
                            "real-video-v2-artifacts.sha256"
                        ),
                        "artifact_manifest_sha256": hashlib.sha256(
                            real_manifest.read_bytes()
                        ).hexdigest(),
                    },
                },
            }
        )
    )
    committed_mot = tmp_path / (
        "evidence/motchallenge-v1/corpora/motchallenge-v1-artifacts.sha256"
    )
    committed_real = tmp_path / (
        "evidence/motchallenge-v1/corpora/real-video-v2-artifacts.sha256"
    )
    committed_mot.parent.mkdir(parents=True)
    committed_mot.write_bytes(mot_canonical)
    committed_real.write_bytes(real_manifest.read_bytes())
    assert len(verify_corpus_manifests(tmp_path, real_manifest)) == 2
    real_manifest.write_text(f"{'d' * 64}  rvmot-a1c9/frames/frame-000000.jpg\n")
    with pytest.raises(RuntimeError, match="real-video artifact manifest differs"):
        verify_corpus_manifests(tmp_path, real_manifest)


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
