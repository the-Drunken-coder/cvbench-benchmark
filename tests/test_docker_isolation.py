import json
import os
import stat
from pathlib import Path
from subprocess import Popen
from unittest.mock import MagicMock, patch

import pytest

from cvbench.config import load_system
from cvbench.runner import _restrict_socket_access
from cvbench.runtime import ResolvedImage, StartedRuntime, start_runtime, verify_docker_isolation

ROOT = Path(__file__).parents[1]
EXPECTED_UID = os.getuid() or 65532
EXPECTED_GID = os.getgid() if os.getuid() else 65532


def test_docker_command_mounts_only_socket_and_disables_network(tmp_path: Path) -> None:
    config = load_system(ROOT / "systems/example-good-docker.yaml")
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir()
    socket_path = socket_dir / "input.sock"
    socket_path.touch()
    _restrict_socket_access(socket_dir, socket_path)
    with (
        patch(
            "cvbench.runtime._resolve_image",
            return_value=ResolvedImage("registry.example/good@sha256:immutable", "sha256:image-id"),
        ),
        patch(
            "cvbench.runtime._docker_accounting_scope",
            return_value=(
                "/cvbench-test",
                "cvbench-test",
                Path("/sys/fs/cgroup/cvbench-test"),
            ),
        ),
        patch("cvbench.runtime.subprocess.Popen") as popen,
    ):
        popen.return_value = MagicMock(spec=Popen)
        runtime = start_runtime(config, socket_dir, tmp_path)
    command = runtime.command
    assert "--rm" not in command
    assert command[command.index("--cgroup-parent") + 1] == "/cvbench-test"
    assert command[command.index("--name") + 1].startswith("cvbench-")
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "2048m"
    assert command[command.index("--memory-swap") + 1] == "2048m"
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[command.index("--user") + 1] == f"{EXPECTED_UID}:{EXPECTED_GID}"
    assert command[command.index("--volume") + 1] == f"{socket_dir}:/run/cvbench"
    assert str(ROOT) not in " ".join(command)
    assert "registry.example/good@sha256:immutable" in command
    assert config.image not in command
    assert runtime.resolved_image_id == "sha256:image-id"
    assert runtime.accounting_cgroup_name == "cvbench-test"
    assert runtime.accounting_cgroup_path == Path("/sys/fs/cgroup/cvbench-test")
    assert runtime.container_name == command[command.index("--name") + 1]
    assert runtime.isolation["status"] == "pending_verification"
    assert runtime.isolation["expected_mount"] == {
        "source": str(socket_dir.resolve()),
        "destination": "/run/cvbench",
    }
    assert runtime.isolation["expected_container_user"] == f"{EXPECTED_UID}:{EXPECTED_GID}"
    assert runtime.isolation["future_frame_isolation"] is None
    assert runtime.isolation["ground_truth_access"] is None
    assert runtime.isolation["repository_access"] is None
    assert runtime.isolation["media_access"] is None
    assert runtime.isolation["image_identity_verified"] is None
    assert runtime.isolation["socket_access"] == {
        "owner_uid": EXPECTED_UID,
        "owner_gid": EXPECTED_GID,
        "directory_mode": "0o700",
        "socket_mode": "0o600",
    }


def test_control_plane_job_id_adds_a_unique_docker_label(tmp_path: Path) -> None:
    config = load_system(ROOT / "systems/example-good-docker.yaml")
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir()
    socket_path = socket_dir / "input.sock"
    socket_path.touch()
    _restrict_socket_access(socket_dir, socket_path)
    job_id = "12345678-1234-4123-8123-123456789abc"
    with (
        patch.dict(os.environ, {"CVBENCH_DOCKER_JOB_ID": job_id}),
        patch(
            "cvbench.runtime._resolve_image",
            return_value=ResolvedImage("registry.example/good@sha256:immutable", "sha256:image-id"),
        ),
        patch(
            "cvbench.runtime._docker_accounting_scope",
            return_value=(
                "/cvbench-test",
                "cvbench-test",
                Path("/sys/fs/cgroup/cvbench-test"),
            ),
        ),
        patch("cvbench.runtime.subprocess.Popen") as popen,
    ):
        popen.return_value = MagicMock(spec=Popen)
        runtime = start_runtime(config, socket_dir, tmp_path)

    label_index = runtime.command.index("--label")
    assert runtime.command[label_index + 1] == f"cvbench.control-plane-job={job_id}"


def test_docker_inspection_distinguishes_applied_limits(tmp_path: Path) -> None:
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("abc")
    process = MagicMock(spec=Popen)
    runtime = StartedRuntime(
        process=process,
        cidfile=cidfile,
        resolved_image="sha256:image",
        resolved_image_id="sha256:image-id",
        command=[],
        isolation={
            "requested": {"cpu_limit": 4, "memory_limit_mb": 2048, "network_access": False},
            "status": "pending_verification",
            "future_frame_isolation": True,
            "expected_container_user": f"{EXPECTED_UID}:{EXPECTED_GID}",
            "socket_access": {
                "owner_uid": EXPECTED_UID,
                "owner_gid": EXPECTED_GID,
                "directory_mode": "0o700",
                "socket_mode": "0o600",
            },
            "expected_mount": {"source": str(tmp_path / "socket"), "destination": "/run/cvbench"},
            "image_identity": {
                "configured_reference": "good:latest",
                "resolved_reference": "sha256:image",
                "resolved_image_id": "sha256:image-id",
                "executed_reference": None,
                "executed_image_id": None,
            },
        },
    )
    inspected = [
        {
            "HostConfig": {
                "NanoCpus": 4_000_000_000,
                "Memory": 2048 * 1024 * 1024,
                "MemorySwap": 2048 * 1024 * 1024,
                "PidsLimit": 512,
                "NetworkMode": "none",
            },
            "Mounts": [{"Source": str(tmp_path / "socket"), "Destination": "/run/cvbench"}],
            "Image": "sha256:image-id",
            "Config": {"Image": "sha256:image", "User": f"{EXPECTED_UID}:{EXPECTED_GID}"},
        }
    ]
    result = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=result):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    assert evidence["status"] == "verified"
    assert evidence["applied"] == {
        "cpu_limit": 4.0,
        "memory_limit_mb": 2048.0,
        "memory_swap_limit_mb": 2048.0,
        "pids_limit": 512,
    }
    assert evidence["future_frame_isolation"] is True
    assert evidence["ground_truth_access"] is False
    assert evidence["repository_access"] is False
    assert evidence["media_access"] is False
    assert evidence["image_identity_verified"] is True
    assert evidence["container_user_alignment_verified"] is True
    assert evidence["image_identity"]["executed_image_id"] == "sha256:image-id"

    inspected[0]["Image"] = "sha256:different-image"
    mismatch = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=mismatch):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    assert evidence["status"] == "verification_failed"
    assert evidence["image_identity_verified"] is None
    assert evidence["future_frame_isolation"] is None
    assert evidence["ground_truth_access"] is None
    assert evidence["repository_access"] is None
    assert evidence["media_access"] is None
    assert evidence["network_mode"] is None
    assert evidence["mounts"] is None

    inspected[0]["Image"] = "sha256:image-id"
    inspected[0]["Mounts"][0]["Source"] = str(tmp_path / "wrong-socket")
    wrong_mount = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=wrong_mount):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    assert evidence["status"] == "verification_failed"
    assert evidence["future_frame_isolation"] is None
    assert evidence["ground_truth_access"] is None
    assert evidence["repository_access"] is None
    assert evidence["media_access"] is None
    assert evidence["image_identity_verified"] is None

    inspected[0]["Mounts"][0]["Source"] = str(tmp_path / "socket")
    inspected[0]["Config"]["User"] = "999:999"
    wrong_user = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=wrong_user):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    assert evidence["status"] == "verification_failed"
    assert evidence["container_user_alignment_verified"] is None
    inspected[0]["Config"]["User"] = f"{EXPECTED_UID}:{EXPECTED_GID}"
    with patch("cvbench.runtime.subprocess.run", return_value=result):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    assert evidence["status"] == "verified"
    assert "error" not in evidence

    inspected[0]["HostConfig"]["MemorySwap"] = 4096 * 1024 * 1024
    wrong_swap = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=wrong_swap):
        assert verify_docker_isolation(runtime, tmp_path / "socket")["status"] == "verification_failed"
    inspected[0]["HostConfig"]["MemorySwap"] = 2048 * 1024 * 1024

    inspected[0]["HostConfig"]["PidsLimit"] = 1024
    wrong_pids = MagicMock(returncode=0, stdout=json.dumps(inspected), stderr="")
    with patch("cvbench.runtime.subprocess.run", return_value=wrong_pids):
        assert verify_docker_isolation(runtime, tmp_path / "socket")["status"] == "verification_failed"


def _minimal_verification_runtime(tmp_path: Path, *, with_cidfile: bool = True) -> StartedRuntime:
    cidfile = tmp_path / "container.cid" if with_cidfile else None
    if cidfile is not None:
        cidfile.write_text("abc")
    return StartedRuntime(
        process=MagicMock(spec=Popen),
        cidfile=cidfile,
        resolved_image="sha256:image",
        resolved_image_id="sha256:image-id",
        command=[],
        isolation={
            "requested": {"cpu_limit": 4, "memory_limit_mb": 2048, "network_access": False},
            "status": "pending_verification",
            "future_frame_isolation": True,
            "ground_truth_access": False,
            "repository_access": False,
            "media_access": False,
            "expected_container_user": f"{EXPECTED_UID}:{EXPECTED_GID}",
            "expected_mount": {"source": str(tmp_path / "socket"), "destination": "/run/cvbench"},
            "image_identity": {
                "configured_reference": "good:latest",
                "resolved_reference": "sha256:image",
                "resolved_image_id": "sha256:image-id",
                "executed_reference": "stale",
                "executed_image_id": "sha256:stale",
            },
        },
    )


def _assert_unknown_verification_claims(evidence: dict[str, object]) -> None:
    assert evidence["status"] == "verification_failed"
    assert evidence["error"]
    for key in (
        "future_frame_isolation",
        "ground_truth_access",
        "repository_access",
        "media_access",
        "mounts",
        "network_mode",
        "applied",
        "image_identity_verified",
        "executed_container_user",
        "container_user_alignment_verified",
    ):
        assert evidence[key] is None, key
    assert evidence["image_identity"]["executed_reference"] is None
    assert evidence["image_identity"]["executed_image_id"] is None


def test_missing_cidfile_keeps_verification_claims_unknown(tmp_path: Path) -> None:
    evidence = verify_docker_isolation(_minimal_verification_runtime(tmp_path, with_cidfile=False), tmp_path / "socket")
    _assert_unknown_verification_claims(evidence)


@pytest.mark.parametrize(
    "result",
    [
        MagicMock(returncode=1, stdout="", stderr="inspect failed"),
        MagicMock(returncode=0, stdout="not-json", stderr=""),
        MagicMock(returncode=0, stdout="{}", stderr=""),
    ],
    ids=["inspect-failure", "malformed-json", "malformed-record"],
)
def test_inspection_failures_keep_verification_claims_unknown(
    tmp_path: Path, result: MagicMock
) -> None:
    runtime = _minimal_verification_runtime(tmp_path)
    with patch("cvbench.runtime.subprocess.run", return_value=result):
        evidence = verify_docker_isolation(runtime, tmp_path / "socket")
    _assert_unknown_verification_claims(evidence)


def test_benchmark_socket_permissions_are_owner_only(tmp_path: Path) -> None:
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir()
    socket_path = socket_dir / "input.sock"
    socket_path.touch()
    _restrict_socket_access(socket_dir, socket_path)
    assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert socket_dir.stat().st_uid == os.getuid()
    assert socket_path.stat().st_uid == os.getuid()


def test_example_image_does_not_copy_scenarios_or_workspace() -> None:
    dockerfile = (ROOT / "examples/Dockerfile.good").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert "COPY ." not in dockerfile
    assert "scenarios" not in dockerfile
    assert "USER cvbench" in dockerfile
    assert "COPY schemas /app/schemas" in dockerfile
    assert dockerignore.splitlines()[0] == "*"
    assert "!src/**" in dockerignore
    assert "!schemas/*.schema.json" in dockerignore


def test_real_video_image_has_no_scenario_or_media_mount_contract() -> None:
    dockerfile = (ROOT / "examples/Dockerfile.real-video-baseline").read_text()
    assert "COPY ." not in dockerfile
    assert "scenarios" not in dockerfile
    assert "data/" not in dockerfile
    assert "real_video_baseline" in dockerfile
