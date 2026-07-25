#!/usr/bin/env python3
"""Lease and execute at most one trusted CVBench control-plane job."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
PUBLIC_BENCHMARK_VERSION = "2.0.0"
PUBLIC_BENCHMARK_MANIFEST = "benchmarks/public-whole-system-v2.yaml"
PUBLIC_TIMING_COMPUTE_CONTRACT = "cvbench.timing-compute/v1"
PUBLIC_DELIVERY_POLICY = "cvbench.delivery-lossless/v1"
PUBLIC_REPLAY_PROFILE = "native"
PUBLIC_REPLAY_RATE = 1
PUBLIC_LEADERBOARD_POLICY = "cvbench.pareto/v1"
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
) -> tuple[int, dict[str, Any] | None]:
    url = f"{base_url.rstrip('/')}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("CVBENCH_API_BASE_URL must use HTTPS (except localhost development)")
    payload = None if body is None else callback_payload_bytes(body)
    request = urllib.request.Request(
        url,
        data=payload or b"",
        method="POST",
        headers={
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
    max_result_bytes = lease_data.get("max_result_bytes", MAX_CALLBACK_BYTES)
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("lease contains an invalid submission id")
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise ValueError("lease contains an invalid immutable image reference")
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
        "resources": {"cpu_limit": 4, "memory_limit_mb": 2048, "network_access": False},
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


def execute_submission(repository: Path, submission: dict[str, Any], work: Path) -> dict[str, Any]:
    image = submission["image"]
    environment = sanitized_environment()
    job_id = str(submission["id"])
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("submission ID is invalid")
    environment["CVBENCH_DOCKER_JOB_ID"] = job_id
    try:
        hydrate(repository)
        subprocess.run(
            ["docker", "pull", "--platform", "linux/amd64", image],
            cwd=repository,
            env=environment,
            timeout=600,
            check=True,
        )
        system_config = work / "submitted-system.json"
        runs = work / "runs"
        write_system_config(system_config, submission)
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
        reported_scenarios = report.get("provenance", {}).get("comparison_inputs", {}).get("scenarios", [])
        reported_ids = (
            [scenario.get("id") for scenario in reported_scenarios]
            if isinstance(reported_scenarios, list)
            else []
        )
        if (
            not isinstance(reported_scenarios, list)
            or len(reported_scenarios) != len(PUBLIC_SCENARIO_IDS)
            or not all(isinstance(scenario, dict) for scenario in reported_scenarios)
            or len(set(reported_ids)) != len(reported_ids)
            or set(reported_ids) != PUBLIC_SCENARIO_IDS
        ):
            raise RuntimeError("benchmark report scenario set does not match the assigned public suite")
        isolation = report.get("runtime_isolation", {})
        if isolation.get("status") != "verified" or isolation.get("network_mode") != "none":
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
    try:
        with tempfile.TemporaryDirectory(prefix="cvbench-job-") as temporary:
            report = execute_submission(repository, submission, Path(temporary))
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
        api_request(base_url, runner_token, path, body=success_body)
    except Exception as exc:
        print(f"Success callback for CVBench submission {submission['id']} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Completed CVBench submission {submission['id']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
