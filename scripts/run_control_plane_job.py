#!/usr/bin/env python3
"""Lease and execute at most one trusted CVBench control-plane job."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cvbench.audit import AUDIT_EVIDENCE_MAX_BYTES
from cvbench.json_contract import serialized_json_bytes
from cvbench.reporting import validate_report

try:
    from scripts.hydrate_real_video_corpus import hydrate
except ModuleNotFoundError:  # Direct `python scripts/run_control_plane_job.py` execution.
    from hydrate_real_video_corpus import hydrate

IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[a-f0-9]{64}$"
)
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")
DOCKER_JOB_LABEL = "cvbench.control-plane-job"
SECRET_ENVIRONMENT_KEYS = {
    "CVBENCH_RUNNER_TOKEN",
    "RUNNER_TOKEN",
    "SUBMISSION_API_KEYS",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}
MAX_CALLBACK_BYTES = 1024 * 1024
PUBLIC_BENCHMARK_ID = "public-whole-system-tracking"
PUBLIC_BENCHMARK_VERSION = "3.0.0"
PUBLIC_BENCHMARK_MANIFEST = "benchmarks/public-whole-system-v3.yaml"
PUBLIC_TIMING_COMPUTE_CONTRACT = "cvbench.timing-compute/v1"
PUBLIC_DELIVERY_POLICY = "cvbench.delivery-lossless/v1"
PUBLIC_REPLAY_PROFILE = "native"
PUBLIC_REPLAY_RATE = 1
PUBLIC_LEADERBOARD_POLICY = "cvbench.pareto/v1"
PUBLIC_RESOURCES = {"cpu_limit": 4, "memory_limit_mb": 8192, "network_access": False}
PUBLIC_CONTAINER_GUARDS = {"memory_swap_limit_mb": 8192, "pids_limit": 512}
PUBLIC_RUN_BUDGETS = {
    "max_run_seconds": 240,
    "max_drain_seconds": 10,
    "max_output_records": 250000,
    "max_output_line_bytes": 1000000,
    "max_total_output_bytes": 100000000,
    "max_output_records_per_second": 10000,
}
PUBLIC_SCENARIO_IDS = {
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
}


def callback_payload_bytes(body: dict[str, Any]) -> bytes:
    return serialized_json_bytes(body)


def api_request(
    base_url: str,
    token: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    method: str = "POST",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    url = f"{base_url.rstrip('/')}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("CVBENCH_API_BASE_URL must use HTTPS (except localhost development)")
    payload = None if body is None else callback_payload_bytes(body)
    request = urllib.request.Request(
        url,
        data=payload or b"",
        method=method,
        headers={
            **(headers or {}),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "cvbench-trusted-runner/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None
    except urllib.error.HTTPError as exc:
        content = exc.read()
        detail = content.decode(errors="replace")[:1000]
        raise RuntimeError(f"control-plane request failed ({exc.code}): {detail}") from exc


def retry_api_request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any] | None]:
    for attempt in range(3):
        try:
            return api_request(*args, **kwargs)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
        except RuntimeError as exc:
            if attempt == 2 or not re.search(r"control-plane request failed \((?:429|5\d\d)\)", str(exc)):
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def validate_lease(lease: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
    submission = lease.get("submission")
    lease_data = lease.get("lease")
    if not isinstance(submission, dict) or not isinstance(lease_data, dict):
        raise ValueError("lease response is missing submission or lease")
    job_id = submission.get("id")
    image = submission.get("image")
    argv = submission.get("argv")
    token = lease_data.get("token")
    benchmark = submission.get("benchmark")
    transport = submission.get("transport", {"type": "registry"})
    max_result_bytes = lease_data.get("max_result_bytes", MAX_CALLBACK_BYTES)
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("lease contains an invalid submission id")
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("lease contains an invalid immutable image reference")
    if not isinstance(transport, dict) or transport.get("type") not in {"registry", "uploaded_oci"}:
        raise ValueError("lease contains an invalid image transport")
    if transport.get("type") == "uploaded_oci" and (
        not re.fullmatch(r"[a-f0-9]{64}", str(transport.get("archive_sha256", "")))
        or not isinstance(transport.get("archive_size"), int)
        or isinstance(transport.get("archive_size"), bool)
        or not 1 <= transport["archive_size"] <= 8 * 1024 * 1024 * 1024
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(transport.get("image_id", "")))
        or transport.get("download_path") != f"/api/v1/internal/submissions/{job_id}/artifact"
    ):
        raise ValueError("lease contains invalid uploaded OCI metadata")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 32
        or not all(isinstance(arg, str) and 1 <= len(arg) <= 256 and not has_control_characters(arg) for arg in argv)
    ):
        raise ValueError("lease contains invalid argv")
    if not isinstance(token, str) or not 32 <= len(token) <= 200:
        raise ValueError("lease token is invalid")
    if not isinstance(benchmark, dict) or (
        benchmark.get("id") != PUBLIC_BENCHMARK_ID
        or benchmark.get("version") != PUBLIC_BENCHMARK_VERSION
        or benchmark.get("manifest") != PUBLIC_BENCHMARK_MANIFEST
        or benchmark.get("timing_compute_contract") != PUBLIC_TIMING_COMPUTE_CONTRACT
        or benchmark.get("delivery_policy") != PUBLIC_DELIVERY_POLICY
        or benchmark.get("replay_profile") != PUBLIC_REPLAY_PROFILE
        or benchmark.get("replay_rate") != PUBLIC_REPLAY_RATE
        or benchmark.get("leaderboard_policy") != PUBLIC_LEADERBOARD_POLICY
        or benchmark.get("resources") != PUBLIC_RESOURCES
        or benchmark.get("container_guards") != PUBLIC_CONTAINER_GUARDS
        or benchmark.get("run_budgets") != PUBLIC_RUN_BUDGETS
    ):
        raise ValueError("lease contains an unsupported benchmark assignment")
    if (
        not isinstance(max_result_bytes, int)
        or isinstance(max_result_bytes, bool)
        or not 16 * 1024 <= max_result_bytes <= MAX_CALLBACK_BYTES
    ):
        raise ValueError("lease result byte limit is invalid")
    return submission, token, max_result_bytes


def build_success_callback(report: dict[str, Any], lease_token: str, max_bytes: int) -> dict[str, Any]:
    audit_evidence = report.get("audit_evidence")
    if audit_evidence is not None and len(serialized_json_bytes(audit_evidence)) > AUDIT_EVIDENCE_MAX_BYTES:
        raise ValueError("audit_evidence exceeds the serialized evidence budget")
    body = {"status": "succeeded", "lease_token": lease_token, "report": report}
    if len(callback_payload_bytes(body)) <= max_bytes:
        return body

    compact_report = dict(report)
    timing = report.get("timing")
    delivery = timing.get("delivery") if isinstance(timing, dict) else None
    per_frame = delivery.get("per_frame") if isinstance(delivery, dict) else None
    if isinstance(per_frame, list):
        retained_count = min(64, len(per_frame))
        head = (retained_count + 1) // 2
        tail = retained_count // 2
        compact_timing = dict(timing)
        compact_delivery = dict(delivery)
        compact_report["timing"] = compact_timing
        compact_timing["delivery"] = compact_delivery
        compact_delivery["per_frame"] = per_frame[:head] + (
            per_frame[-tail:] if tail else []
        )
        compact_delivery["per_frame_compaction"] = {
            "truncated": retained_count < len(per_frame),
            "retention": "head_and_tail",
            "original_frames": len(per_frame),
            "retained_frames": retained_count,
            "omitted_frames": len(per_frame) - retained_count,
        }
        body = {
            "status": "succeeded",
            "lease_token": lease_token,
            "report": compact_report,
        }
        if len(callback_payload_bytes(body)) <= max_bytes:
            return body

    diagnostics = compact_report.get("diagnostics")
    stderr = diagnostics.get("sut_stderr") if isinstance(diagnostics, dict) else None
    if not isinstance(stderr, list) or not all(isinstance(line, str) for line in stderr):
        raise ValueError("report exceeds the callback budget without compactable stderr diagnostics")

    compact_diagnostics = dict(diagnostics)
    compact_report["diagnostics"] = compact_diagnostics
    compact_diagnostics["sut_stderr"] = []
    compact_diagnostics["sut_stderr_compaction"] = {
        "truncated": True,
        "retention": "head_and_tail",
        "original_lines": len(stderr),
        "retained_lines": 0,
        "omitted_lines": len(stderr),
        "original_utf8_bytes": sum(len(line.encode()) for line in stderr),
    }

    body = {"status": "succeeded", "lease_token": lease_token, "report": compact_report}
    if len(callback_payload_bytes(body)) > max_bytes:
        raise ValueError("score-critical report exceeds the callback budget after diagnostic compaction")

    def retain_stderr(line_count: int) -> bool:
        head = (line_count + 1) // 2
        tail = line_count // 2
        compact_diagnostics["sut_stderr"] = stderr[:head] + (stderr[-tail:] if tail else [])
        compact_diagnostics["sut_stderr_compaction"].update(
            {"retained_lines": line_count, "omitted_lines": len(stderr) - line_count}
        )
        return len(callback_payload_bytes(body)) <= max_bytes

    low, high = 0, len(stderr)
    while low < high:
        retained = (low + high + 1) // 2
        if retain_stderr(retained):
            low = retained
        else:
            high = retained - 1

    if not retain_stderr(low):
        raise ValueError("compacted report exceeds the callback budget")
    return body


def has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def sanitized_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in SECRET_ENVIRONMENT_KEYS}


def write_system_config(path: Path, submission: dict[str, Any]) -> None:
    config = {
        "schema_version": "cvbench.system/v1",
        "id": f"control-plane-{submission['id']}",
        "revision": str(submission.get("model", {}).get("version", "submitted")),
        "runtime": {"type": "docker", "image": submission["image"], "command": submission["argv"]},
        "readiness": {"type": "stdout_pattern", "pattern": "CVBENCH_READY", "timeout_seconds": 30},
        "shutdown": {"grace_period_seconds": 10},
        "resources": PUBLIC_RESOURCES,
    }
    path.write_text(json.dumps(config, indent=2) + "\n")


def _containers_for_job(job_id: str, environment: dict[str, str]) -> list[str]:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("submission ID is invalid")
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={DOCKER_JOB_LABEL}={job_id}"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
        check=True,
    )
    container_ids = [value.strip() for value in result.stdout.splitlines() if value.strip()]
    if not all(CONTAINER_ID_PATTERN.fullmatch(value) for value in container_ids):
        raise RuntimeError("Docker returned an invalid container ID during cleanup")
    return container_ids


def cleanup_benchmark_containers(job_id: str, environment: dict[str, str]) -> int:
    container_ids = _containers_for_job(job_id, environment)
    if container_ids:
        subprocess.run(
            ["docker", "rm", "--force", *container_ids],
            env=environment,
            timeout=30,
            check=True,
        )
    if _containers_for_job(job_id, environment):
        raise RuntimeError("a benchmark container survived forced cleanup")
    return len(container_ids)


def execute_submission(
    repository: Path,
    submission: dict[str, Any],
    work: Path,
    *,
    base_url: str | None = None,
    runner_token: str | None = None,
    lease_token: str | None = None,
) -> dict[str, Any]:
    image = submission["image"]
    environment = sanitized_environment()
    job_id = str(submission["id"])
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("submission ID is invalid")
    environment["CVBENCH_DOCKER_JOB_ID"] = job_id
    try:
        transport = submission.get("transport", {"type": "registry"})
        if transport.get("type") == "uploaded_oci":
            if not base_url or not runner_token or not lease_token:
                raise ValueError("uploaded OCI execution requires control-plane credentials")
            report_progress(
                base_url,
                runner_token,
                job_id,
                lease_token,
                "artifact_download",
                "Downloading the immutable image archive.",
            )
            archive = work / "submitted-image.docker.tar.gz"
            download_submission_artifact(base_url, runner_token, submission, archive)
            report_progress(
                base_url,
                runner_token,
                job_id,
                lease_token,
                "image_load",
                "Loading and verifying the linux/amd64 image.",
            )
            subprocess.run(
                ["docker", "load", "--input", str(archive)],
                cwd=repository,
                env=environment,
                timeout=600,
                check=True,
            )
            image = transport["image_id"]
            inspected = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", image],
                capture_output=True,
                text=True,
                cwd=repository,
                env=environment,
                timeout=30,
                check=True,
            ).stdout.strip()
            if inspected != f"{image} linux/amd64":
                raise RuntimeError("uploaded archive does not contain the declared linux/amd64 image")
        else:
            if base_url and runner_token and lease_token:
                report_progress(
                    base_url,
                    runner_token,
                    job_id,
                    lease_token,
                    "image_load",
                    "Pulling and verifying the immutable registry image.",
                )
            subprocess.run(
                ["docker", "pull", "--platform", "linux/amd64", image],
                cwd=repository,
                env=environment,
                timeout=600,
                check=True,
            )
        if base_url and runner_token and lease_token:
            report_progress(
                base_url,
                runner_token,
                job_id,
                lease_token,
                "corpus_preparation",
                "Verifying the pinned benchmark corpus.",
            )
        hydrate(repository)
        system_config = work / "submitted-system.json"
        runs = work / "runs"
        write_system_config(system_config, {**submission, "image": image})
        if base_url and runner_token and lease_token:
            report_progress(
                base_url,
                runner_token,
                job_id,
                lease_token,
                "benchmark_running",
                "Running the fixed 16-scenario benchmark.",
            )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "cvbench.cli",
                "run",
                "--benchmark",
                str(repository / PUBLIC_BENCHMARK_MANIFEST),
                "--system",
                str(system_config),
                "--output",
                str(runs),
            ],
            cwd=repository,
            env=environment,
            timeout=1500,
            check=True,
        )
        reports = list(runs.glob("*/report.json"))
        if len(reports) != 1:
            raise RuntimeError(f"expected exactly one report, found {len(reports)}")
        report = json.loads(reports[0].read_text())
        if report.get("outcome", {}).get("status") != "completed":
            raise RuntimeError(f"benchmark outcome was {report.get('outcome', {}).get('status', 'unknown')}")
        benchmark = report.get("benchmark", {})
        if benchmark.get("id") != PUBLIC_BENCHMARK_ID or benchmark.get("version") != PUBLIC_BENCHMARK_VERSION:
            raise RuntimeError("benchmark report does not match the assigned public suite")
        provenance = report.get("provenance", {})
        comparison_inputs = provenance.get("comparison_inputs", {})
        if (
            provenance.get("benchmark_path") != PUBLIC_BENCHMARK_MANIFEST
            or comparison_inputs.get("benchmark_id") != PUBLIC_BENCHMARK_ID
            or comparison_inputs.get("benchmark_version") != PUBLIC_BENCHMARK_VERSION
            or comparison_inputs.get("resource_envelope", {}).get("benchmark") != PUBLIC_RESOURCES
            or comparison_inputs.get("resource_envelope", {}).get("system") != PUBLIC_RESOURCES
            or comparison_inputs.get("run_budgets") != PUBLIC_RUN_BUDGETS
            or provenance.get("resource_envelope", {}).get("benchmark") != PUBLIC_RESOURCES
            or provenance.get("resource_envelope", {}).get("system") != PUBLIC_RESOURCES
            or provenance.get("run_budgets") != PUBLIC_RUN_BUDGETS
        ):
            raise RuntimeError("benchmark report does not preserve the assigned public resource contract")
        reported_scenarios = comparison_inputs.get("scenarios", [])
        reported_ids = (
            [scenario.get("id") for scenario in reported_scenarios]
            if isinstance(reported_scenarios, list)
            else []
        )
        evaluation_ids = provenance.get("evaluation_order", {}).get("scenario_ids", [])
        if (
            not isinstance(reported_scenarios, list)
            or len(reported_scenarios) != len(PUBLIC_SCENARIO_IDS)
            or not all(isinstance(scenario, dict) for scenario in reported_scenarios)
            or len(set(reported_ids)) != len(reported_ids)
            or set(reported_ids) != PUBLIC_SCENARIO_IDS
            or not isinstance(evaluation_ids, list)
            or len(evaluation_ids) != len(PUBLIC_SCENARIO_IDS)
            or len(set(evaluation_ids)) != len(evaluation_ids)
            or set(evaluation_ids) != PUBLIC_SCENARIO_IDS
        ):
            raise RuntimeError("benchmark report scenario set does not match the assigned public suite")
        isolation = report.get("runtime_isolation", {})
        expected_applied = {
            "cpu_limit": float(PUBLIC_RESOURCES["cpu_limit"]),
            "memory_limit_mb": float(PUBLIC_RESOURCES["memory_limit_mb"]),
            **PUBLIC_CONTAINER_GUARDS,
        }
        expected_requested = {**PUBLIC_RESOURCES, **PUBLIC_CONTAINER_GUARDS}
        if (
            isolation.get("status") != "verified"
            or isolation.get("network_mode") != "none"
            or isolation.get("requested") != expected_requested
            or isolation.get("applied") != expected_applied
        ):
            raise RuntimeError("benchmark did not verify the required container isolation")
        report["runner"] = {
            "schema_version": "cvbench.runner/v1",
            "commit": os.environ.get("GITHUB_SHA"),
            "workflow_run_url": _workflow_run_url(),
            "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        }
        validate_report(report)
        return report
    finally:
        cleanup_benchmark_containers(job_id, environment)


def _workflow_run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    safe_values = (server, repository, run_id)
    if server and repository and run_id and all("\n" not in value and "\r" not in value for value in safe_values):
        return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"
    return None


def callback_path(submission_id: str) -> str:
    if not JOB_ID_PATTERN.fullmatch(submission_id):
        raise ValueError("submission ID is invalid")
    return f"/api/v1/internal/submissions/{submission_id}/result"


def download_submission_artifact(
    base_url: str,
    runner_token: str,
    submission: dict[str, Any],
    destination: Path,
) -> None:
    transport = submission["transport"]
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{transport['download_path']}",
        method="GET",
        headers={
            "Authorization": f"Bearer {runner_token}",
            "User-Agent": "cvbench-trusted-runner/1",
        },
    )
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=600) as response, destination.open("wb") as output:
        if response.headers.get("x-cvbench-archive-sha256") != transport["archive_sha256"]:
            raise RuntimeError("artifact response checksum metadata does not match the lease")
        if response.headers.get("x-cvbench-image-id") != transport["image_id"]:
            raise RuntimeError("artifact response image identity does not match the lease")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > transport["archive_size"]:
                raise RuntimeError("artifact response exceeded its declared byte count")
            digest.update(chunk)
            output.write(chunk)
    if size != transport["archive_size"] or digest.hexdigest() != transport["archive_sha256"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError("artifact bytes do not match the declared size and checksum")


def report_progress(
    base_url: str,
    runner_token: str,
    submission_id: str,
    lease_token: str,
    stage: str,
    message: str,
    completed: int = 0,
) -> None:
    try:
        retry_api_request(
            base_url,
            runner_token,
            f"/api/v1/internal/submissions/{submission_id}/progress",
            body={
                "lease_token": lease_token,
                "stage": stage,
                "message": message,
                "completed": completed,
                "total": len(PUBLIC_SCENARIO_IDS),
            },
        )
    except Exception as exc:
        print(f"Progress update skipped: {exc}", file=sys.stderr)


def upload_prediction_overlays(
    base_url: str,
    runner_token: str,
    submission_id: str,
    lease_token: str,
    work: Path,
    *,
    progress: bool = False,
) -> None:
    overlay_dirs = list((work / "runs").glob("*/prediction-overlays"))
    if len(overlay_dirs) != 1:
        raise RuntimeError(f"expected exactly one prediction overlay directory, found {len(overlay_dirs)}")
    paths = sorted(overlay_dirs[0].glob("*.json"))
    if {path.stem for path in paths} != PUBLIC_SCENARIO_IDS:
        raise RuntimeError("prediction overlay set does not match the assigned public suite")
    for completed, path in enumerate(paths, start=1):
        payload = json.loads(path.read_text())
        retry_api_request(
            base_url,
            runner_token,
            f"/api/v1/internal/submissions/{submission_id}/prediction-overlays/{path.stem}",
            body=payload,
            method="PUT",
            headers={"X-CVBench-Lease-Token": lease_token},
        )
        if progress:
            report_progress(
                base_url,
                runner_token,
                submission_id,
                lease_token,
                "publishing_playback",
                f"Publishing model playback {completed} of {len(paths)}.",
                completed,
            )
    retry_api_request(
        base_url,
        runner_token,
        f"/api/v1/internal/submissions/{submission_id}/prediction-overlays/complete",
        body={"lease_token": lease_token},
    )


def main() -> int:
    base_url = os.environ.get("CVBENCH_API_BASE_URL", "").strip()
    runner_token = os.environ.get("CVBENCH_RUNNER_TOKEN", "").strip()
    if not base_url or not runner_token:
        raise SystemExit("CVBENCH_API_BASE_URL and CVBENCH_RUNNER_TOKEN are required")

    status, lease = api_request(base_url, runner_token, "/api/v1/internal/leases")
    if status == 204 or lease is None:
        print("No queued CVBench submissions.")
        return 0

    submission, lease_token, max_result_bytes = validate_lease(lease)
    path = callback_path(submission["id"])
    repository = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="cvbench-job-") as temporary:
        try:
            temporary_path = Path(temporary)
            report = execute_submission(
                repository,
                submission,
                temporary_path,
                base_url=base_url,
                runner_token=runner_token,
                lease_token=lease_token,
            )
            success_body = build_success_callback(report, lease_token, max_result_bytes)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:2000]
            try:
                api_request(
                    base_url,
                    runner_token,
                    path,
                    body={"status": "failed", "lease_token": lease_token, "error": error},
                )
            except Exception as callback_error:
                print(f"Result callback also failed: {callback_error}", file=sys.stderr)
            print(f"CVBench submission {submission['id']} failed: {error}", file=sys.stderr)
            return 1
        try:
            upload_prediction_overlays(
                base_url,
                runner_token,
                submission["id"],
                lease_token,
                temporary_path,
                progress=True,
            )
        except Exception as publication_error:
            print(
                f"Prediction overlay publication for CVBench submission {submission['id']} failed; "
                f"the lease will retry without recording a system failure: {publication_error}",
                file=sys.stderr,
            )
            return 1
    try:
        api_request(base_url, runner_token, path, body=success_body)
    except Exception as exc:
        print(f"Success callback for CVBench submission {submission['id']} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Completed CVBench submission {submission['id']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
