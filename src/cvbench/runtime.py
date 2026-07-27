from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import SystemConfig
from .errors import RuntimeFailure

UNPRIVILEGED_SOCKET_UID = 65532
CONTROL_PLANE_JOB_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
CGROUP_ROOT = Path("/sys/fs/cgroup")
ACCOUNTING_CGROUP_NAME_PATTERN = re.compile(r"cvbench-[0-9A-Za-z.-]{1,120}(?:\.slice)?")
RETENTION_CGROUP_NAME = "cvbench-retain"
DOCKER_PIDS_LIMIT = 512


@dataclass
class StartedRuntime:
    process: subprocess.Popen[str]
    cidfile: Path | None
    resolved_image: str | None
    command: list[str]
    isolation: dict[str, object]
    process_group_id: int | None = None
    resolved_image_id: str | None = None
    accounting_cgroup_parent: str | None = None
    accounting_cgroup_name: str | None = None
    accounting_cgroup_path: Path | None = None
    container_name: str | None = None


@dataclass(frozen=True)
class ResolvedImage:
    reference: str
    image_id: str


def not_started_isolation(config: SystemConfig, error: str) -> dict[str, object]:
    """Describe requested isolation without claiming that any runtime fact was observed."""
    memory_limit = config.resources.get("memory_limit_mb")
    return {
        "runtime": config.runtime_type,
        "requested": {
            "cpu_limit": config.resources.get("cpu_limit"),
            "memory_limit_mb": memory_limit,
            "network_access": config.resources.get("network_access", False),
            "memory_swap_limit_mb": memory_limit if config.runtime_type == "docker" else None,
            "pids_limit": DOCKER_PIDS_LIMIT if config.runtime_type == "docker" else None,
        },
        "status": "not_started",
        "future_frame_isolation": None,
        "ground_truth_access": None,
        "repository_access": None,
        "media_access": None,
        "container_id": None,
        "mounts": None,
        "network_mode": None,
        "applied": None,
        "image_identity_verified": None,
        "executed_container_user": None,
        "container_user_alignment_verified": None,
        "expected_container_user": None,
        "socket_access": None,
        "expected_mount": None,
        "image_identity": {
            "configured_reference": config.image,
            "resolved_reference": None,
            "resolved_image_id": None,
            "executed_reference": None,
            "executed_image_id": None,
        },
        "error": error,
    }


@dataclass(frozen=True)
class RuntimeStop:
    exit_code: int | None
    forced: bool
    scoring_timed_out: bool
    scoring_finished_ns: int
    teardown_finished_ns: int


def _align_docker_socket_owner(socket_dir: Path) -> tuple[int, int]:
    socket_path = socket_dir / "input.sock"
    owner = socket_path.stat()
    uid, gid = owner.st_uid, owner.st_gid
    if uid == 0:
        uid = gid = UNPRIVILEGED_SOCKET_UID
        os.chown(socket_dir, uid, gid)
        os.chown(socket_path, uid, gid)
    return uid, gid


def _resolve_image(image: str) -> ResolvedImage:
    if not shutil.which("docker"):
        raise RuntimeFailure("Docker runtime requested, but docker is not installed")
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}|{{.Id}}", image],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeFailure(f"Docker image is unavailable: {image}: {result.stderr.strip()}")
    digests, _, image_id = result.stdout.strip().partition("|")
    if not image_id.startswith("sha256:"):
        raise RuntimeFailure(f"could not resolve an immutable image ID for Docker image {image}")
    if digests not in {"", "null", "[]"}:
        values = json.loads(digests)
        if values:
            return ResolvedImage(str(values[0]), image_id)
    return ResolvedImage(image_id, image_id)


def _runtime_token(run_dir: Path) -> str:
    token = re.sub(r"[^0-9A-Za-z.-]", "-", run_dir.name)[:100]
    return f"cvbench-{token}"


def _docker_accounting_scope(run_dir: Path) -> tuple[str | None, str | None, Path | None]:
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        return None, None, None
    result = subprocess.run(
        ["docker", "info", "--format", "{{.CgroupDriver}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeFailure(result.stderr.strip() or "could not determine Docker cgroup driver")
    name = _runtime_token(run_dir)
    driver = result.stdout.strip()
    if driver == "cgroupfs":
        return f"/{name}", name, CGROUP_ROOT / name
    if driver == "systemd":
        name = f"{name}.slice"
        return name, name, CGROUP_ROOT / name
    raise RuntimeFailure(f"unsupported Docker cgroup driver for authoritative accounting: {driver}")


def start_runtime(config: SystemConfig, socket_dir: Path, run_dir: Path) -> StartedRuntime:
    environment = os.environ.copy()
    environment.update(config.environment)
    environment["CVBENCH_INPUT_SOCKET"] = str(socket_dir / "input.sock")
    cidfile: Path | None = None
    resolved_image: str | None = None
    resolved_image_id: str | None = None
    container_user: str | None = None
    socket_access: dict[str, object] | None = None
    accounting_cgroup_parent: str | None = None
    accounting_cgroup_name: str | None = None
    accounting_cgroup_path: Path | None = None
    container_name: str | None = None
    if config.runtime_type == "local":
        command = [sys.executable if value == "{python}" else value for value in config.command]
        cwd = config.path.parent.parent
    else:
        assert config.image is not None
        resolution = _resolve_image(config.image)
        resolved_image = resolution.reference
        resolved_image_id = resolution.image_id
        socket_uid, socket_gid = _align_docker_socket_owner(socket_dir)
        container_user = f"{socket_uid}:{socket_gid}"
        socket_access = {
            "owner_uid": socket_uid,
            "owner_gid": socket_gid,
            "directory_mode": oct(socket_dir.stat().st_mode & 0o777),
            "socket_mode": oct((socket_dir / "input.sock").stat().st_mode & 0o777),
        }
        cidfile = run_dir / "container.cid"
        container_name = _runtime_token(run_dir)
        (
            accounting_cgroup_parent,
            accounting_cgroup_name,
            accounting_cgroup_path,
        ) = _docker_accounting_scope(run_dir)
        command = [
            "docker",
            "run",
            "--cidfile",
            str(cidfile),
            "--name",
            container_name,
            "--network",
            "none",
            "--user",
            container_user,
            "--volume",
            f"{socket_dir}:/run/cvbench",
            "--env",
            "CVBENCH_INPUT_SOCKET=/run/cvbench/input.sock",
        ]
        if accounting_cgroup_parent is not None:
            command.extend(["--cgroup-parent", accounting_cgroup_parent])
        cpu_limit = config.resources.get("cpu_limit")
        memory_limit = config.resources.get("memory_limit_mb")
        if cpu_limit:
            command.extend(["--cpus", str(cpu_limit)])
        if memory_limit:
            command.extend(
                [
                    "--memory",
                    f"{memory_limit}m",
                    "--memory-swap",
                    f"{memory_limit}m",
                ]
            )
        command.extend(["--pids-limit", str(DOCKER_PIDS_LIMIT)])
        control_plane_job_id = os.environ.get("CVBENCH_DOCKER_JOB_ID")
        if control_plane_job_id:
            if not CONTROL_PLANE_JOB_ID_PATTERN.fullmatch(control_plane_job_id):
                raise RuntimeFailure("CVBENCH_DOCKER_JOB_ID must be a UUIDv4")
            command.extend(["--label", f"cvbench.control-plane-job={control_plane_job_id}"])
        for key, value in sorted(config.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend([resolved_image, *config.command])
        cwd = config.path.parent.parent
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=config.runtime_type == "local",
        )
    except OSError as exc:
        raise RuntimeFailure(f"could not start SUT: {exc}") from exc
    isolation: dict[str, object] = {
        "runtime": config.runtime_type,
        "requested": {
            "cpu_limit": config.resources.get("cpu_limit"),
            "memory_limit_mb": config.resources.get("memory_limit_mb"),
            "network_access": config.resources.get("network_access", False),
            "memory_swap_limit_mb": (
                config.resources.get("memory_limit_mb") if config.runtime_type == "docker" else None
            ),
            "pids_limit": DOCKER_PIDS_LIMIT if config.runtime_type == "docker" else None,
        },
        "status": "not_enforced_local" if config.runtime_type == "local" else "pending_verification",
        "future_frame_isolation": None,
        "ground_truth_access": None,
        "repository_access": None,
        "media_access": None,
        "container_id": None,
        "mounts": None,
        "network_mode": None,
        "applied": None,
        "image_identity_verified": None,
        "executed_container_user": None,
        "container_user_alignment_verified": None,
        "expected_container_user": container_user,
        "socket_access": socket_access,
        "expected_mount": (
            {"source": str(socket_dir), "destination": "/run/cvbench"}
            if config.runtime_type == "docker"
            else None
        ),
        "image_identity": {
            "configured_reference": config.image,
            "resolved_reference": resolved_image,
            "resolved_image_id": resolved_image_id,
            "executed_reference": None,
            "executed_image_id": None,
        },
    }
    return StartedRuntime(
        process,
        cidfile,
        resolved_image,
        command,
        isolation,
        process.pid if config.runtime_type == "local" else None,
        resolved_image_id,
        accounting_cgroup_parent,
        accounting_cgroup_name,
        accounting_cgroup_path,
        container_name,
    )


def _signal_process_group(runtime: StartedRuntime, sig: signal.Signals) -> None:
    if runtime.process_group_id is None:
        if runtime.process.poll() is None:
            runtime.process.send_signal(sig)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(runtime.process_group_id, sig)


def stop_runtime(
    runtime: StartedRuntime,
    grace: float,
    checkpoint: Callable[[], None] | None = None,
    on_scoring_finished: Callable[[], None] | None = None,
    release_after_scoring: Callable[[], None] | None = None,
    scoring_complete: Callable[[], bool] | None = None,
) -> RuntimeStop:
    """Enforce the scoring deadline, then tear down descendants out of band."""
    forced = False
    scoring_timed_out = False
    deadline = time.monotonic() + max(0, grace)
    exit_code = runtime.process.poll()
    scoring_done = False
    while time.monotonic() < deadline:
        if scoring_complete is None:
            if exit_code is not None:
                break
        else:
            scoring_done = scoring_complete()
            if scoring_done or exit_code not in {None, 0}:
                break
        if checkpoint is not None:
            checkpoint()
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        exit_code = runtime.process.poll()
    if scoring_complete is not None and not scoring_done:
        scoring_done = scoring_complete()
        scoring_timed_out = not scoring_done and exit_code in {None, 0}
    if on_scoring_finished is not None:
        on_scoring_finished()
    elif checkpoint is not None:
        checkpoint()
    scoring_finished_ns = time.monotonic_ns()
    if release_after_scoring is not None:
        release_after_scoring()
        if exit_code is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                exit_code = runtime.process.wait(timeout=0.25)
    if exit_code is None:
        forced = True
        _signal_process_group(runtime, signal.SIGTERM)
        try:
            exit_code = runtime.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _signal_process_group(runtime, signal.SIGKILL)
            exit_code = runtime.process.wait(timeout=2)
    if runtime.process_group_id is not None:
        # The direct child may exit while descendants remain. They are still
        # runner-owned and must not survive any benchmark outcome.
        _signal_process_group(runtime, signal.SIGTERM)
        time.sleep(0.02)
        _signal_process_group(runtime, signal.SIGKILL)
    return RuntimeStop(
        exit_code,
        forced,
        scoring_timed_out,
        scoring_finished_ns,
        time.monotonic_ns(),
    )


def verify_docker_isolation(runtime: StartedRuntime, socket_dir: Path, timeout: float = 10) -> dict[str, object]:
    _reset_verification_claims(runtime.isolation)
    if runtime.cidfile is None:
        return _verification_failed(runtime, "container ID file is unavailable")
    deadline = time.monotonic() + timeout
    container_id = ""
    while time.monotonic() < deadline:
        if runtime.cidfile.exists():
            try:
                container_id = runtime.cidfile.read_text().strip()
            except OSError as exc:
                return _verification_failed(runtime, f"container ID file could not be read: {exc}")
            if container_id:
                break
        time.sleep(0.02)
    if not container_id:
        return _verification_failed(runtime, "container ID was not created")
    try:
        result = subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _verification_failed(runtime, f"docker inspect failed: {exc}")
    if result.returncode:
        return _verification_failed(runtime, result.stderr.strip() or "docker inspect failed")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError("docker inspect returned an invalid container record")
        inspected = payload[0]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _verification_failed(runtime, f"malformed docker inspect output: {exc}")
    host = inspected.get("HostConfig", {})
    mounts = inspected.get("Mounts", [])
    container_config = inspected.get("Config", {})
    if not isinstance(host, dict) or not isinstance(mounts, list) or not isinstance(container_config, dict):
        return _verification_failed(runtime, "malformed docker inspect fields")
    if not all(isinstance(mount, dict) for mount in mounts):
        return _verification_failed(runtime, "malformed docker inspect mounts")
    mount_pairs = [{"source": item.get("Source"), "destination": item.get("Destination")} for item in mounts]
    requested = runtime.isolation.get("requested")
    if not isinstance(requested, dict):
        return _verification_failed(runtime, "runtime isolation request metadata is malformed")
    expected_cpu = requested.get("cpu_limit")
    expected_memory = requested.get("memory_limit_mb")
    cpu_applied = _scaled_number(host.get("NanoCpus"), 1_000_000_000)
    memory_applied = _scaled_number(host.get("Memory"), 1024 * 1024)
    memory_swap_applied = _scaled_number(host.get("MemorySwap"), 1024 * 1024)
    executed_image_id = inspected.get("Image")
    executed_reference = container_config.get("Image")
    executed_user = container_config.get("User")
    image_identity = runtime.isolation.get("image_identity")
    if not isinstance(image_identity, dict):
        return _verification_failed(runtime, "runtime image identity metadata is malformed")
    identity_ok = (
        executed_image_id == runtime.resolved_image_id and executed_reference == runtime.resolved_image
    )
    user_ok = executed_user == runtime.isolation.get("expected_container_user")
    expected_mount = runtime.isolation.get("expected_mount")
    expected_mount_ok = (
        isinstance(expected_mount, dict)
        and expected_mount.get("destination") == "/run/cvbench"
        and Path(str(expected_mount.get("source"))).resolve() == socket_dir.resolve()
    )
    mount_ok = expected_mount_ok and mount_pairs == [expected_mount]
    network_ok = host.get("NetworkMode") == "none"
    limits_ok = (
        (expected_cpu is None or float(expected_cpu) == cpu_applied)
        and (
            expected_memory is None
            or (
                float(expected_memory) == memory_applied
                and float(expected_memory) == memory_swap_applied
            )
        )
        and host.get("PidsLimit") == DOCKER_PIDS_LIMIT
    )
    if not (mount_ok and network_ok and limits_ok and identity_ok and user_ok):
        return _verification_failed(runtime, "Docker isolation inspection did not satisfy every required claim")
    image_identity.update(
        {
            "executed_reference": executed_reference,
            "executed_image_id": executed_image_id,
        }
    )
    runtime.isolation.update(
        {
            "status": "verified",
            "container_id": container_id,
            "mounts": mount_pairs,
            "network_mode": host.get("NetworkMode"),
            "applied": {
                "cpu_limit": cpu_applied,
                "memory_limit_mb": memory_applied,
                "memory_swap_limit_mb": memory_swap_applied,
                "pids_limit": host.get("PidsLimit"),
            },
            "future_frame_isolation": mount_ok and network_ok,
            "ground_truth_access": not (mount_ok and network_ok),
            "repository_access": not (mount_ok and network_ok),
            "media_access": not (mount_ok and network_ok and len(mount_pairs) == 1),
            "image_identity_verified": identity_ok,
            "executed_container_user": executed_user,
            "container_user_alignment_verified": user_ok,
        }
    )
    return runtime.isolation


def _scaled_number(value: object, divisor: float) -> float | None:
    if value is None or value == 0:
        return None
    try:
        return float(value) / divisor
    except (TypeError, ValueError):
        return None


def _reset_verification_claims(isolation: dict[str, object]) -> None:
    isolation.update(
        {
            "container_id": None,
            "mounts": None,
            "network_mode": None,
            "applied": None,
            "future_frame_isolation": None,
            "ground_truth_access": None,
            "repository_access": None,
            "media_access": None,
            "image_identity_verified": None,
            "executed_container_user": None,
            "container_user_alignment_verified": None,
        }
    )
    isolation.pop("error", None)
    image_identity = isolation.get("image_identity")
    if isinstance(image_identity, dict):
        image_identity.update({"executed_reference": None, "executed_image_id": None})


def _verification_failed(runtime: StartedRuntime, error: str) -> dict[str, object]:
    _reset_verification_claims(runtime.isolation)
    runtime.isolation.update({"status": "verification_failed", "error": error})
    return runtime.isolation


def _remove_accounting_cgroup(path: Path, expected_name: str, cgroup_root: Path) -> str | None:
    root = cgroup_root.resolve()
    candidate = path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return f"refused to remove accounting cgroup outside {root}"
    if (
        candidate == root
        or candidate.name != expected_name
        or not ACCOUNTING_CGROUP_NAME_PATTERN.fullmatch(candidate.name)
    ):
        return "refused to remove an unexpected accounting cgroup"
    for _ in range(3):
        try:
            candidate.rmdir()
            return None
        except FileNotFoundError:
            return None
        except OSError:
            time.sleep(0.05)
    if shutil.which("sudo"):
        try:
            result = subprocess.run(
                ["sudo", "-n", "rmdir", "--", str(candidate)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 or not candidate.exists():
                return None
        except (OSError, subprocess.SubprocessError):
            pass
    return f"accounting cgroup cleanup failed: {candidate}"


def cleanup_runtime(
    runtime: StartedRuntime,
    accounting_cgroup_path: Path | None = None,
    *,
    cgroup_root: Path = CGROUP_ROOT,
) -> list[str]:
    errors: list[str] = []
    container_id = ""
    docker = shutil.which("docker")
    if runtime.cidfile is not None and runtime.cidfile.exists():
        try:
            container_id = runtime.cidfile.read_text().strip()
        except OSError as exc:
            errors.append(f"container ID file cleanup read failed: {exc}")
    container_identifier = container_id or runtime.container_name or ""
    if container_identifier and docker:
        cleanup_detail = ""
        for _ in range(3):
            try:
                subprocess.run(
                    ["docker", "rm", "--force", container_identifier],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                inspected = subprocess.run(
                    ["docker", "inspect", container_identifier],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_detail = str(exc)
                time.sleep(0.05)
                continue
            if inspected.returncode != 0:
                break
            time.sleep(0.05)
        else:
            suffix = f": {cleanup_detail}" if cleanup_detail else ""
            errors.append(f"container cleanup failed: {container_identifier}{suffix}")
    elif container_identifier:
        errors.append("container cleanup failed: docker is unavailable")
    if accounting_cgroup_path is not None and runtime.accounting_cgroup_name is not None:
        retention_error = _remove_accounting_cgroup(
            accounting_cgroup_path / RETENTION_CGROUP_NAME,
            RETENTION_CGROUP_NAME,
            cgroup_root,
        )
        if retention_error is not None:
            errors.append(retention_error)
        error = _remove_accounting_cgroup(
            accounting_cgroup_path,
            runtime.accounting_cgroup_name,
            cgroup_root,
        )
        if error is not None and error not in errors:
            errors.append(error)
    return errors
