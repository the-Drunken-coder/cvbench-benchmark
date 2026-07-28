from __future__ import annotations

import json
from pathlib import Path

import pytest

import cvbench.agent as agent
from cvbench.agent import (
    AgentProject,
    AgentSubmissionError,
    BuiltImage,
    ControlPlaneClient,
    CredentialStore,
    ImageArchive,
    agent_result,
    load_agent_project,
    submit_project,
)


def test_agent_project_uses_a_tiny_manifest_and_rejects_escape_paths(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.agent").write_text("FROM scratch\n")
    (tmp_path / "cvbench.yaml").write_text(
        "\n".join(
            [
                "schema_version: cvbench.agent/v1",
                "name: woodland-tracker",
                "version: iteration-3",
                "dockerfile: Dockerfile.agent",
                "command: [python, -m, tracker]",
            ]
        )
        + "\n"
    )
    project = load_agent_project(tmp_path)
    assert project.name == "woodland-tracker"
    assert project.version == "iteration-3"
    assert project.command == ["python", "-m", "tracker"]
    assert project.dockerfile == tmp_path / "Dockerfile.agent"

    (tmp_path / "cvbench.yaml").write_text("dockerfile: ../outside\n")
    with pytest.raises(AgentSubmissionError, match="inside the project"):
        load_agent_project(tmp_path)


def test_credential_fallback_is_owner_only_and_environment_can_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = CredentialStore("https://cvbench.test")
    store.save("local-agent-token")
    credential_path = tmp_path / "cvbench" / "cvbench-test.json"
    assert credential_path.stat().st_mode & 0o777 == 0o600
    assert store.load() == "local-agent-token"

    monkeypatch.setenv("CVBENCH_API_KEY", "environment-token")
    assert store.load() == "environment-token"


def test_submit_project_is_one_idempotent_machine_readable_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = AgentProject(
        root=tmp_path,
        context=tmp_path,
        dockerfile=tmp_path / "Dockerfile",
        name="Agent model",
        version="4",
        command=None,
    )
    image = BuiltImage(
        reference="cvbench-agent/model:test",
        image_id=f"sha256:{'a' * 64}",
        command=["python", "-m", "tracker"],
    )
    archive_path = tmp_path / "image.tar.gz"
    archive_path.write_bytes(b"archive")
    archive = ImageArchive(path=archive_path, sha256="b" * 64, size=7)
    monkeypatch.setattr(agent, "build_project", lambda *_args, **_kwargs: image)
    monkeypatch.setattr(agent, "archive_image", lambda *_args, **_kwargs: archive)
    client = FakeClient()

    result = submit_project(project, client, wait=True)

    assert client.created_submission["artifact_id"] == "artifact-id"
    assert client.created_submission["command"] == image.command
    assert client.created_submission["idempotency_key"].startswith("agent-")
    assert result["schema_version"] == "cvbench.agent-result/v1"
    assert result["status"] == "succeeded"
    assert result["feedback"]["verdict"] == "iterate"
    assert result["result_url"].endswith("/results/?submission=submission-id")


def test_agent_result_keeps_the_exact_public_record() -> None:
    record = {
        "id": "submission-id",
        "status": "failed",
        "progress": {"stage": "failed"},
        "error": "bad protocol",
        "result": None,
    }
    output = agent_result(record, "https://cvbench.test/")
    assert output["error"] == "bad protocol"
    assert output["record"] == record
    assert json.loads(json.dumps(output)) == output


def test_wait_has_a_bounded_deadline_and_keeps_the_result_link(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ControlPlaneClient("https://cvbench.test", "")
    monkeypatch.setattr(client, "submission", lambda _submission_id: {"status": "running"})
    with pytest.raises(AgentSubmissionError, match=r"https://cvbench\.test/results/\?submission=submission-id"):
        client.wait("submission-id", timeout_seconds=0)


class FakeClient:
    api_url = "https://cvbench.test"

    def __init__(self) -> None:
        self.created_submission = {}

    def create_artifact(self, archive: ImageArchive, image_id: str) -> dict[str, object]:
        return {"id": "artifact-id", "part_size": 16 * 1024 * 1024}

    def upload_artifact(self, artifact: dict[str, object], archive: ImageArchive, *, progress) -> dict[str, object]:
        progress(archive.size, archive.size)
        return {**artifact, "status": "ready"}

    def create_submission(
        self,
        artifact_id: str,
        project: AgentProject,
        command: list[str],
        idempotency_key: str,
    ) -> dict[str, object]:
        self.created_submission = {
            "artifact_id": artifact_id,
            "project": project,
            "command": command,
            "idempotency_key": idempotency_key,
        }
        return {"id": "submission-id", "status": "queued"}

    def wait(self, submission_id: str, *, progress) -> dict[str, object]:
        record = {
            "id": submission_id,
            "status": "succeeded",
            "progress": {"stage": "completed", "completed": 16, "total": 16},
            "result": {
                "scores": {"observed_coverage": 0.8},
                "findings": [{"finding_id": "TRACK-QUALITY-001"}],
                "agent_feedback": {"verdict": "iterate"},
            },
        }
        progress(record)
        return record
