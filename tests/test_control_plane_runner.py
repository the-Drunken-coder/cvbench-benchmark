from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cvbench.audit import build_audit_evidence
from cvbench.config import Thresholds
from cvbench.json_contract import serialized_json_bytes
from cvbench.metrics import calculate_metrics
from scripts.run_control_plane_job import (
    IMAGE_PATTERN,
    MAX_CALLBACK_BYTES,
    PUBLIC_BENCHMARK_ID,
    PUBLIC_BENCHMARK_MANIFEST,
    PUBLIC_BENCHMARK_VERSION,
    PUBLIC_CONTAINER_GUARDS,
    PUBLIC_DELIVERY_POLICY,
    PUBLIC_LEADERBOARD_POLICY,
    PUBLIC_REPLAY_PROFILE,
    PUBLIC_REPLAY_RATE,
    PUBLIC_RESOURCES,
    PUBLIC_RUN_BUDGETS,
    PUBLIC_SCENARIO_IDS,
    PUBLIC_TIMING_COMPUTE_CONTRACT,
    SECRET_ENVIRONMENT_KEYS,
    build_success_callback,
    callback_path,
    callback_payload_bytes,
    cleanup_benchmark_containers,
    download_submission_artifact,
    execute_submission,
    load_uploaded_image,
    main,
    report_progress,
    retry_api_request,
    sanitized_environment,
    upload_prediction_overlays,
    validate_lease,
    write_system_config,
)
from tests.helpers import gt, output

IMAGE = f"ghcr.io/example/tracker@sha256:{'a' * 64}"
BENCHMARK = {
    "id": PUBLIC_BENCHMARK_ID,
    "version": PUBLIC_BENCHMARK_VERSION,
    "manifest": PUBLIC_BENCHMARK_MANIFEST,
    "timing_compute_contract": PUBLIC_TIMING_COMPUTE_CONTRACT,
    "delivery_policy": PUBLIC_DELIVERY_POLICY,
    "replay_profile": PUBLIC_REPLAY_PROFILE,
    "replay_rate": PUBLIC_REPLAY_RATE,
    "leaderboard_policy": PUBLIC_LEADERBOARD_POLICY,
    "resources": PUBLIC_RESOURCES,
    "container_guards": PUBLIC_CONTAINER_GUARDS,
    "run_budgets": PUBLIC_RUN_BUDGETS,
}


def test_prediction_overlay_uploads_exact_suite_then_seals(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "runs" / "run-1" / "prediction-overlays"
    overlay_dir.mkdir(parents=True)
    for scenario_id in PUBLIC_SCENARIO_IDS:
        (overlay_dir / f"{scenario_id}.json").write_text(json.dumps({"scenario_id": scenario_id}))
    with patch("scripts.run_control_plane_job.api_request", return_value=(201, {})) as request:
        upload_prediction_overlays(
            "https://cvbench.test",
            "runner-token",
            "12345678-1234-4123-8123-123456789abc",
            "b" * 64,
            tmp_path,
        )
    assert request.call_count == len(PUBLIC_SCENARIO_IDS) + 1
    assert all(call.kwargs["method"] == "PUT" for call in request.call_args_list[:-1])
    assert all(
        call.kwargs["headers"]["X-CVBench-Lease-Token"] == "b" * 64
        for call in request.call_args_list[:-1]
    )
    assert request.call_args_list[-1].kwargs["body"] == {"lease_token": "b" * 64}


def test_retry_api_request_retries_only_transient_http_failures() -> None:
    with (
        patch(
            "scripts.run_control_plane_job.api_request",
            side_effect=[RuntimeError("control-plane request failed (503): busy"), (201, {})],
        ) as request,
        patch("scripts.run_control_plane_job.time.sleep") as sleep,
    ):
        assert retry_api_request("https://cvbench.test", "token", "/overlay") == (201, {})
    assert request.call_count == 2
    sleep.assert_called_once_with(1)

    with (
        patch(
            "scripts.run_control_plane_job.api_request",
            side_effect=RuntimeError("control-plane request failed (400): invalid"),
        ) as request,
        pytest.raises(RuntimeError, match=r"\(400\)"),
    ):
        retry_api_request("https://cvbench.test", "token", "/overlay")
    assert request.call_count == 1


@pytest.mark.parametrize(
    "transport_error",
    [urllib.error.URLError("temporary DNS failure"), TimeoutError("socket timed out")],
)
def test_retry_api_request_retries_transport_failures(transport_error: OSError) -> None:
    with (
        patch(
            "scripts.run_control_plane_job.api_request",
            side_effect=[transport_error, (201, {})],
        ) as request,
        patch("scripts.run_control_plane_job.time.sleep") as sleep,
    ):
        assert retry_api_request("https://cvbench.test", "token", "/overlay") == (201, {})
    assert request.call_count == 2
    sleep.assert_called_once_with(1)


def test_prediction_overlay_upload_rejects_missing_directories_and_scenarios(tmp_path: Path) -> None:
    args = (
        "https://cvbench.test",
        "runner-token",
        "12345678-1234-4123-8123-123456789abc",
        "b" * 64,
    )
    with pytest.raises(RuntimeError, match="exactly one"):
        upload_prediction_overlays(*args, tmp_path)

    overlay_dir = tmp_path / "runs" / "run-1" / "prediction-overlays"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "synthetic-acquisition.json").write_text("{}")
    with pytest.raises(RuntimeError, match="does not match"):
        upload_prediction_overlays(*args, tmp_path)


def test_image_pattern_requires_digest_and_rejects_shell_like_input() -> None:
    assert IMAGE_PATTERN.fullmatch(IMAGE)
    assert not IMAGE_PATTERN.fullmatch("ghcr.io/example/tracker:latest")
    assert not IMAGE_PATTERN.fullmatch(f"ghcr.io/example/tracker@sha256:{'A' * 64}")
    assert not IMAGE_PATTERN.fullmatch(f"ghcr.io/example/tracker;curl@sha256:{'a' * 64}")


def test_validate_lease_revalidates_untrusted_control_plane_data() -> None:
    submission, token, max_result_bytes = validate_lease(
        {
            "submission": {
                "id": "12345678-1234-4123-8123-123456789abc",
                "image": IMAGE,
                "argv": ["python", "-m", "tracker"],
                "benchmark": BENCHMARK,
            },
            "lease": {"token": "b" * 64},
        }
    )
    assert submission["image"] == IMAGE
    assert submission["benchmark"] == BENCHMARK
    assert token == "b" * 64
    assert max_result_bytes == MAX_CALLBACK_BYTES

    uploaded_submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": f"cvbench.local/upload@sha256:{'c' * 64}",
        "argv": ["python"],
        "benchmark": BENCHMARK,
        "transport": {
            "type": "uploaded_oci",
            "archive_sha256": "d" * 64,
            "archive_size": 1234,
            "image_id": f"sha256:{'c' * 64}",
            "download_path": "/api/v1/internal/submissions/12345678-1234-4123-8123-123456789abc/artifact",
        },
    }
    validated, _, _ = validate_lease({
        "submission": uploaded_submission,
        "lease": {"token": "b" * 64},
    })
    assert validated["transport"]["type"] == "uploaded_oci"
    broken_transport = {
        **uploaded_submission,
        "transport": {**uploaded_submission["transport"], "archive_sha256": "../bad"},
    }
    with pytest.raises(ValueError, match="uploaded OCI"):
        validate_lease({"submission": broken_transport, "lease": {"token": "b" * 64}})
    boolean_size = {
        **uploaded_submission,
        "transport": {**uploaded_submission["transport"], "archive_size": True},
    }
    with pytest.raises(ValueError, match="uploaded OCI"):
        validate_lease({"submission": boolean_size, "lease": {"token": "b" * 64}})

    with pytest.raises(ValueError, match="argv"):
        validate_lease(
            {
                "submission": {
                    "id": "12345678-1234-4123-8123-123456789abc",
                    "image": IMAGE,
                    "argv": ["python\nmalicious"],
                    "benchmark": BENCHMARK,
                },
                "lease": {"token": "b" * 64},
            }
        )

    with pytest.raises(ValueError, match="submission id"):
        validate_lease(
            {
                "submission": {"id": "../other-job", "image": IMAGE, "argv": ["python"], "benchmark": BENCHMARK},
                "lease": {"token": "b" * 64},
            }
        )

    with pytest.raises(ValueError, match="benchmark assignment"):
        validate_lease(
            {
                "submission": {
                    "id": "12345678-1234-4123-8123-123456789abc",
                    "image": IMAGE,
                    "argv": ["python"],
                    "benchmark": {"id": "other", "version": "1.0.0", "manifest": "benchmarks/other.yaml"},
                },
                "lease": {"token": "b" * 64},
            }
        )

    for key, value in (
        ("timing_compute_contract", "other"),
        ("delivery_policy", "other"),
        ("replay_profile", "half-speed"),
        ("replay_rate", 0.5),
        ("leaderboard_policy", "other"),
        ("resources", {**PUBLIC_RESOURCES, "memory_limit_mb": 2048}),
        ("container_guards", {**PUBLIC_CONTAINER_GUARDS, "pids_limit": 1024}),
        ("run_budgets", {**PUBLIC_RUN_BUDGETS, "max_run_seconds": 90}),
    ):
        with pytest.raises(ValueError, match="benchmark assignment"):
            validate_lease(
                {
                    "submission": {
                        "id": "12345678-1234-4123-8123-123456789abc",
                        "image": IMAGE,
                        "argv": ["python"],
                        "benchmark": {**BENCHMARK, key: value},
                    },
                    "lease": {"token": "b" * 64},
                }
            )


def test_generated_system_config_preserves_argv_without_a_shell(tmp_path: Path) -> None:
    path = tmp_path / "system.json"
    write_system_config(
        path,
        {
            "id": "12345678-1234-4123-8123-123456789abc",
            "image": IMAGE,
            "argv": ["python", "-m", "tracker", "--threshold=0.7"],
            "model": {"version": "1"},
        },
    )
    config = json.loads(path.read_text())
    assert config["runtime"] == {
        "type": "docker",
        "image": IMAGE,
        "command": ["python", "-m", "tracker", "--threshold=0.7"],
    }
    assert config["resources"] == PUBLIC_RESOURCES


def test_uploaded_artifact_download_verifies_headers_size_and_sha256(tmp_path: Path) -> None:
    content = b"compressed immutable image"
    archive_sha256 = hashlib.sha256(content).hexdigest()
    submission = {
        "transport": {
            "archive_sha256": archive_sha256,
            "archive_size": len(content),
            "image_id": f"sha256:{'a' * 64}",
            "download_path": "/api/v1/internal/submissions/12345678-1234-4123-8123-123456789abc/artifact",
        }
    }
    response = MagicMock()
    response.headers = {
        "x-cvbench-archive-sha256": archive_sha256,
        "x-cvbench-image-id": f"sha256:{'a' * 64}",
    }
    response.read.side_effect = [content, b""]
    response.__enter__.return_value = response
    destination = tmp_path / "image.tar.gz"
    with patch("scripts.run_control_plane_job.urllib.request.urlopen", return_value=response):
        download_submission_artifact("https://cvbench.test", "runner-token", submission, destination)
    assert destination.read_bytes() == content

    response.read.side_effect = [b"corrupt", b""]
    with (
        patch("scripts.run_control_plane_job.urllib.request.urlopen", return_value=response),
        pytest.raises(RuntimeError, match="size and checksum"),
    ):
        download_submission_artifact("https://cvbench.test", "runner-token", submission, destination)


def test_uploaded_artifact_load_bounds_gzip_expansion(tmp_path: Path) -> None:
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(gzip.compress(b"verified docker tar bytes"))
    process = MagicMock()
    process.stdin = MagicMock()
    process.wait.return_value = 0
    with patch("scripts.run_control_plane_job.subprocess.Popen", return_value=process) as popen:
        load_uploaded_image(archive, tmp_path, {"PATH": "/usr/bin"}, max_expanded_bytes=1024)
    popen.assert_called_once_with(
        ["docker", "load"],
        stdin=subprocess.PIPE,
        cwd=tmp_path,
        env={"PATH": "/usr/bin"},
    )
    assert b"".join(call.args[0] for call in process.stdin.write.call_args_list) == b"verified docker tar bytes"

    process = MagicMock()
    process.stdin = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    with (
        patch("scripts.run_control_plane_job.subprocess.Popen", return_value=process),
        pytest.raises(RuntimeError, match="expanded image archive exceeds"),
    ):
        load_uploaded_image(archive, tmp_path, {"PATH": "/usr/bin"}, max_expanded_bytes=8)
    process.kill.assert_called_once()


def test_progress_failure_is_observable_but_never_fails_scoring(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("scripts.run_control_plane_job.retry_api_request", side_effect=RuntimeError("offline")):
        report_progress(
            "https://cvbench.test",
            "runner-token",
            "12345678-1234-4123-8123-123456789abc",
            "b" * 64,
            "benchmark_running",
            "Running.",
        )
    assert "Progress update skipped" in capsys.readouterr().err


def test_callback_path_and_secret_scrubbing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert callback_path("12345678-1234-4123-8123-123456789abc").endswith("/result")
    with pytest.raises(ValueError):
        callback_path("../other-job")

    for key in SECRET_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("SAFE_VALUE", "kept")
    environment = sanitized_environment()
    assert environment["SAFE_VALUE"] == "kept"
    assert SECRET_ENVIRONMENT_KEYS.isdisjoint(environment)


def test_cleanup_force_removes_only_containers_with_the_unique_job_label() -> None:
    job_id = "12345678-1234-4123-8123-123456789abc"
    container_id = "a" * 64
    listed = MagicMock(stdout=f"{container_id}\n")
    removed = MagicMock()
    empty = MagicMock(stdout="")
    environment = {"PATH": "/usr/bin"}
    with patch("scripts.run_control_plane_job.subprocess.run", side_effect=[listed, removed, empty]) as run:
        assert cleanup_benchmark_containers(job_id, environment) == 1

    assert run.call_args_list[0].args[0] == [
        "docker",
        "ps",
        "-aq",
        "--filter",
        f"label=cvbench.control-plane-job={job_id}",
    ]
    assert run.call_args_list[1].args[0] == ["docker", "rm", "--force", container_id]


def test_execution_timeout_still_runs_unique_label_cleanup(tmp_path: Path) -> None:
    submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": IMAGE,
        "argv": ["python", "-m", "tracker"],
    }
    with (
        patch("scripts.run_control_plane_job.hydrate"),
        patch(
            "scripts.run_control_plane_job.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["docker", "pull"], 600),
        ),
        patch("scripts.run_control_plane_job.cleanup_benchmark_containers") as cleanup,
        pytest.raises(subprocess.TimeoutExpired),
    ):
        execute_submission(tmp_path, submission, tmp_path)

    cleanup.assert_called_once()
    assert cleanup.call_args.args[0] == submission["id"]
    assert cleanup.call_args.args[1]["CVBENCH_DOCKER_JOB_ID"] == submission["id"]


def test_success_callback_build_failure_is_converted_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": IMAGE,
        "argv": ["python", "-m", "tracker"],
    }
    lease = {"submission": {**submission, "benchmark": BENCHMARK}, "lease": {"token": "b" * 64}}
    monkeypatch.setenv("CVBENCH_API_BASE_URL", "https://cvbench.test")
    monkeypatch.setenv("CVBENCH_RUNNER_TOKEN", "runner-token")
    with (
        patch(
            "scripts.run_control_plane_job.api_request",
            side_effect=[(200, lease), (200, {"status": "failed"})],
        ) as request,
        patch(
            "scripts.run_control_plane_job.execute_submission",
            return_value={"audit_evidence": "x" * MAX_CALLBACK_BYTES},
        ),
    ):
        assert main() == 1

    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["body"]["status"] == "failed"


def test_transient_success_callback_failure_never_emits_failed_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": IMAGE,
        "argv": ["python", "-m", "tracker"],
    }
    lease = {"submission": {**submission, "benchmark": BENCHMARK}, "lease": {"token": "b" * 64}}
    monkeypatch.setenv("CVBENCH_API_BASE_URL", "https://cvbench.test")
    monkeypatch.setenv("CVBENCH_RUNNER_TOKEN", "runner-token")
    with (
        patch(
            "scripts.run_control_plane_job.api_request",
            side_effect=[(200, lease), RuntimeError("transient callback failure")],
        ) as request,
        patch("scripts.run_control_plane_job.execute_submission", return_value={"outcome": {"status": "completed"}}),
        patch("scripts.run_control_plane_job.upload_prediction_overlays"),
    ):
        assert main() == 1

    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["body"]["status"] == "succeeded"


def test_overlay_transport_failure_never_emits_failed_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": IMAGE,
        "argv": ["python", "-m", "tracker"],
    }
    lease = {"submission": {**submission, "benchmark": BENCHMARK}, "lease": {"token": "b" * 64}}
    monkeypatch.setenv("CVBENCH_API_BASE_URL", "https://cvbench.test")
    monkeypatch.setenv("CVBENCH_RUNNER_TOKEN", "runner-token")
    with (
        patch("scripts.run_control_plane_job.api_request", return_value=(200, lease)) as request,
        patch("scripts.run_control_plane_job.execute_submission", return_value={"outcome": {"status": "completed"}}),
        patch(
            "scripts.run_control_plane_job.upload_prediction_overlays",
            side_effect=RuntimeError("control-plane request failed (503): busy"),
        ),
    ):
        assert main() == 1

    assert request.call_count == 1


def test_worst_case_stderr_report_fits_callback_budget_and_records_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = json.loads((Path(__file__).parent / "golden/known-report.json").read_text())
    original_metrics = json.loads(json.dumps(report["metrics"]))
    report["diagnostics"]["sut_stderr"] = ["\0" * 4096] * 1000
    lease_token = "b" * 64
    oversized = {"status": "succeeded", "lease_token": lease_token, "report": report}
    assert len(callback_payload_bytes(oversized)) > MAX_CALLBACK_BYTES

    compacted = build_success_callback(report, lease_token, MAX_CALLBACK_BYTES)
    assert len(callback_payload_bytes(compacted)) <= MAX_CALLBACK_BYTES
    assert compacted["report"]["metrics"] == original_metrics
    summary = compacted["report"]["diagnostics"]["sut_stderr_compaction"]
    assert summary["original_lines"] == 1000
    assert 0 < summary["retained_lines"] < 1000
    assert summary["omitted_lines"] == 1000 - summary["retained_lines"]

    submission = {
        "id": "12345678-1234-4123-8123-123456789abc",
        "image": IMAGE,
        "argv": ["python", "-m", "tracker"],
    }
    lease = {
        "submission": {**submission, "benchmark": BENCHMARK},
        "lease": {"token": lease_token, "max_result_bytes": MAX_CALLBACK_BYTES},
    }
    terminal: dict[str, object] = {"status": "running"}

    def control_plane_request(
        _base_url: str,
        _runner_token: str,
        _path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object] | None]:
        if body is None:
            return 200, lease
        assert len(callback_payload_bytes(body)) <= MAX_CALLBACK_BYTES
        terminal.update(status=body["status"], report=body["report"])
        return 200, terminal

    monkeypatch.setenv("CVBENCH_API_BASE_URL", "https://cvbench.test")
    monkeypatch.setenv("CVBENCH_RUNNER_TOKEN", "runner-token")
    with (
        patch("scripts.run_control_plane_job.api_request", side_effect=control_plane_request),
        patch("scripts.run_control_plane_job.execute_submission", return_value=report),
        patch("scripts.run_control_plane_job.upload_prediction_overlays"),
    ):
        assert main() == 0

    assert terminal["status"] == "succeeded"
    assert terminal["report"]["metrics"] == original_metrics


def test_large_per_frame_timing_evidence_is_compacted_without_losing_raw_axes() -> None:
    report = {
        "audit_evidence": {"schema_version": "cvbench.audit/v1"},
        "timing": {
            "source": {"duration_seconds": 10},
            "durations": {"real_time_factor": 1.2},
            "delivery": {
                "per_frame": [
                    {
                        "sequence_id": "sequence",
                        "frame_index": index,
                        "native_source_timestamp_ns": index * 1_000_000,
                        "delivery_backlog_ms": index / 10,
                        "sender_call_ms": 0.1,
                        "deadline_missed": False,
                        "padding": "x" * 200,
                    }
                    for index in range(5000)
                ]
            },
        },
        "resources": {"cpu_seconds_per_native_source_second": 1.25},
    }
    callback = build_success_callback(report, "b" * 64, MAX_CALLBACK_BYTES)
    assert len(callback_payload_bytes(callback)) <= MAX_CALLBACK_BYTES
    compacted = callback["report"]
    assert compacted["timing"]["source"] == report["timing"]["source"]
    assert compacted["timing"]["durations"] == report["timing"]["durations"]
    assert compacted["resources"] == report["resources"]
    delivery = compacted["timing"]["delivery"]
    assert len(delivery["per_frame"]) == 64
    assert delivery["per_frame"][0]["frame_index"] == 0
    assert delivery["per_frame"][-1]["frame_index"] == 4999
    assert delivery["per_frame_compaction"] == {
        "truncated": True,
        "retention": "head_and_tail",
        "original_frames": 5000,
        "retained_frames": 64,
        "omitted_frames": 4936,
    }
    assert len(report["timing"]["delivery"]["per_frame"]) == 5000


def test_near_one_megabyte_model_record_fits_actual_callback_boundary() -> None:
    ground_truth = [gt(0, sequence="near-megabyte")]
    records = [output(0, sequence="near-megabyte", track="😀" * 250_000)]
    metrics, matches = calculate_metrics(ground_truth, records, Thresholds())
    evidence = build_audit_evidence(
        ground_truth,
        records,
        matches,
        metrics,
        {"delivered_frames": 1},
        {"sample_count": 1, "over_time": []},
        {"status": "verified", "network_mode": "none"},
    )
    callback = build_success_callback(
        {"audit_evidence": evidence},
        "b" * 64,
        MAX_CALLBACK_BYTES,
    )

    assert len(serialized_json_bytes({"audit_evidence": records[0].system_record})) > 900_000
    assert len(callback_payload_bytes(callback)) <= MAX_CALLBACK_BYTES
    assert len(serialized_json_bytes(evidence)) <= 256 * 1024


def test_callback_boundary_rejects_unbounded_audit_evidence() -> None:
    with pytest.raises(ValueError, match="audit_evidence exceeds"):
        build_success_callback(
            {"audit_evidence": {"track_id": "😀" * 100_000}},
            "b" * 64,
            MAX_CALLBACK_BYTES,
        )


def test_private_docker_callback_preserves_complete_bounded_audit_evidence() -> None:
    evidence = {
        "schema_version": "cvbench.audit/v1",
        "frame_samples": [
            {
                "ground_truth": [{"count_reason": "matched_observed_and_counted", "bbox_xyxy": [1, 2, 3, 4]}],
                "predictions": [{"track_id": "track-1", "bbox_xyxy": [1, 2, 3, 4]}],
                "matches": [{"iou": 1.0, "target_id": "target-1"}],
            }
        ],
        "score_explanation": {"coverage_denominators": {"observed_coverage": 1}},
        "flags": [{"id": "false_track", "status": "flagged", "review_aid_only": True}],
        "false_track_segments": [{"track_id": "false-track", "duration_ms": 100}],
    }
    report = {
        "system": {"runtime": "docker"},
        "audit_evidence": evidence,
        "metrics": {"sample_counts": {"matches": 1}},
    }
    callback = build_success_callback(report, "b" * 64, MAX_CALLBACK_BYTES)
    assert callback["report"]["audit_evidence"] == evidence
