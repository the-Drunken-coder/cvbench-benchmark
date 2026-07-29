from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_API_URL = "https://cvbench-control-plane.laraujo123546.workers.dev"
ARTIFACT_PART_BYTES = 16 * 1024 * 1024
TERMINAL_STATUSES = {"succeeded", "failed"}
IMAGE_ID_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


class AgentSubmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentProject:
    root: Path
    context: Path
    dockerfile: Path
    name: str
    version: str
    command: list[str] | None


@dataclass(frozen=True)
class BuiltImage:
    reference: str
    image_id: str
    command: list[str]


@dataclass(frozen=True)
class ImageArchive:
    path: Path
    sha256: str
    size: int


def load_agent_project(
    project_path: str | Path,
    *,
    name: str | None = None,
    version: str | None = None,
    dockerfile: str | None = None,
) -> AgentProject:
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise AgentSubmissionError(f"project directory does not exist: {root}")
    manifest_path = root / "cvbench.yaml"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        loaded = yaml.safe_load(manifest_path.read_text())
        if not isinstance(loaded, dict):
            raise AgentSubmissionError("cvbench.yaml must contain a mapping")
        manifest = loaded
        unknown = set(manifest) - {"schema_version", "name", "version", "dockerfile", "context", "command"}
        if unknown:
            raise AgentSubmissionError(f"cvbench.yaml contains unknown fields: {', '.join(sorted(unknown))}")
        if manifest.get("schema_version", "cvbench.agent/v1") != "cvbench.agent/v1":
            raise AgentSubmissionError("cvbench.yaml schema_version must be cvbench.agent/v1")

    context_value = manifest.get("context", ".")
    dockerfile_value = dockerfile or manifest.get("dockerfile", "Dockerfile")
    if not isinstance(context_value, str) or not isinstance(dockerfile_value, str):
        raise AgentSubmissionError("context and dockerfile must be paths")
    context = (root / context_value).resolve()
    dockerfile_path = (root / dockerfile_value).resolve()
    if not context.is_dir() or not context.is_relative_to(root):
        raise AgentSubmissionError("build context must be a directory inside the project")
    if not dockerfile_path.is_file() or not dockerfile_path.is_relative_to(root):
        raise AgentSubmissionError("dockerfile must be a file inside the project")

    project_name = name or manifest.get("name") or root.name
    project_version = version or manifest.get("version") or "development"
    if not isinstance(project_name, str) or not 1 <= len(project_name.strip()) <= 100:
        raise AgentSubmissionError("name must contain 1-100 characters")
    if not isinstance(project_version, (str, int, float)):
        raise AgentSubmissionError("version must be a string or number")
    project_version = str(project_version).strip()
    if not 1 <= len(project_version) <= 100:
        raise AgentSubmissionError("version must contain 1-100 characters")
    command = manifest.get("command")
    if command is not None and (
        not isinstance(command, list)
        or not 1 <= len(command) <= 32
        or not all(isinstance(value, str) and 1 <= len(value) <= 256 for value in command)
    ):
        raise AgentSubmissionError("command must contain 1-32 non-empty string arguments")
    return AgentProject(
        root=root,
        context=context,
        dockerfile=dockerfile_path,
        name=project_name.strip(),
        version=project_version,
        command=list(command) if command else None,
    )


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.returncode == 0


def build_project(project: AgentProject, *, progress: Callable[[str], None] = lambda _message: None) -> BuiltImage:
    if not docker_available():
        raise AgentSubmissionError("Docker is not installed or its daemon is unavailable")
    tag = f"cvbench-agent/{slug(project.name)}:{uuid.uuid4().hex[:12]}"
    progress("Building the linux/amd64 model image.")
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--file",
            str(project.dockerfile),
            "--tag",
            tag,
            str(project.context),
        ],
        cwd=project.root,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=3600,
        check=True,
    )
    inspection = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    values = json.loads(inspection.stdout)
    if not isinstance(values, list) or len(values) != 1:
        raise AgentSubmissionError("Docker returned an unexpected image inspection result")
    value = values[0]
    image_id = value.get("Id")
    if not isinstance(image_id, str) or not IMAGE_ID_PATTERN.fullmatch(image_id):
        raise AgentSubmissionError("Docker did not return an immutable sha256 image ID")
    if value.get("Os") != "linux" or value.get("Architecture") != "amd64":
        raise AgentSubmissionError("built image is not linux/amd64")
    configured = value.get("Config") or {}
    inferred_command = [*(configured.get("Entrypoint") or []), *(configured.get("Cmd") or [])]
    command = project.command or inferred_command
    if (
        not command
        or len(command) > 32
        or not all(isinstance(argument, str) and 1 <= len(argument) <= 256 for argument in command)
    ):
        raise AgentSubmissionError("the image needs a valid ENTRYPOINT/CMD or cvbench.yaml command")
    return BuiltImage(reference=tag, image_id=image_id, command=list(command))


def archive_image(image: BuiltImage, output_dir: Path) -> ImageArchive:
    output = output_dir / "cvbench-image.docker.tar.gz"
    with tempfile.TemporaryFile() as error_output:
        process = subprocess.Popen(
            ["docker", "save", image.image_id],
            stdout=subprocess.PIPE,
            stderr=error_output,
        )
        assert process.stdout is not None
        with output.open("wb") as raw, gzip.GzipFile(
            fileobj=raw,
            mode="wb",
            compresslevel=1,
            mtime=0,
        ) as compressed:
            shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
        returncode = process.wait(timeout=600)
        error_output.seek(0)
        stderr = error_output.read().decode(errors="replace")
    if returncode:
        output.unlink(missing_ok=True)
        raise AgentSubmissionError(f"docker save failed: {stderr[-1000:]}")
    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ImageArchive(path=output, sha256=digest.hexdigest(), size=output.stat().st_size)


class CredentialStore:
    def __init__(self, api_url: str = DEFAULT_API_URL) -> None:
        self.api_url = normalize_api_url(api_url)
        self.account = urllib.parse.urlparse(self.api_url).netloc

    def save(self, token: str) -> None:
        if not token or any(character.isspace() for character in token):
            raise AgentSubmissionError("credential must be one non-empty token without whitespace")
        if platform.system() == "Darwin" and Path("/usr/bin/security").exists():
            subprocess.run(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-a",
                    self.account,
                    "-s",
                    "cvbench-agent",
                    "-w",
                    token,
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            return
        path = self._fallback_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({"api_url": self.api_url, "token": token}) + "\n")
        path.chmod(0o600)

    def load(self) -> str:
        environment_token = os.environ.get("CVBENCH_API_KEY", "").strip()
        if environment_token:
            return environment_token
        if platform.system() == "Darwin" and Path("/usr/bin/security").exists():
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    self.account,
                    "-s",
                    "cvbench-agent",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        path = self._fallback_path()
        if path.exists():
            value = json.loads(path.read_text())
            if value.get("api_url") == self.api_url and isinstance(value.get("token"), str):
                return value["token"]
        raise AgentSubmissionError("not logged in; run `cvbench login` once or set CVBENCH_API_KEY")

    def _fallback_path(self) -> Path:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return root / "cvbench" / f"{slug(self.account)}.json"


class ControlPlaneClient:
    def __init__(self, api_url: str, token: str) -> None:
        self.api_url = normalize_api_url(api_url)
        self.token = token

    def health(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/v1/health", authenticated=False)

    def create_artifact(self, archive: ImageArchive, image_id: str) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/v1/artifacts",
            {
                "archive_sha256": archive.sha256,
                "archive_size": archive.size,
                "image_id": image_id,
                "compression": "gzip",
            },
        )

    def upload_artifact(
        self,
        artifact: dict[str, Any],
        archive: ImageArchive,
        *,
        progress: Callable[[int, int], None] = lambda _sent, _total: None,
    ) -> dict[str, Any]:
        try:
            part_size = int(artifact["part_size"])
        except (KeyError, TypeError, ValueError) as error:
            raise AgentSubmissionError("CVBench returned invalid artifact upload metadata") from error
        if part_size != ARTIFACT_PART_BYTES:
            raise AgentSubmissionError("CVBench returned an unsupported artifact part size")
        sent = 0
        part_number = 1
        with archive.path.open("rb") as handle:
            while chunk := handle.read(part_size):
                self.retry_json(
                    "PUT",
                    f"/api/v1/artifacts/{artifact['id']}/parts/{part_number}",
                    raw=chunk,
                    headers={"content-type": "application/octet-stream"},
                    timeout=180,
                )
                sent += len(chunk)
                progress(sent, archive.size)
                part_number += 1
        return self.retry_json("POST", f"/api/v1/artifacts/{artifact['id']}/complete")

    def create_submission(
        self,
        artifact_id: str,
        project: AgentProject,
        command: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.retry_json(
            "POST",
            "/api/v1/submissions",
            {
                "artifact_id": artifact_id,
                "argv": command,
                "name": project.name,
                "model_version": project.version,
            },
            headers={"idempotency-key": idempotency_key},
        )

    def submission(self, submission_id: str) -> dict[str, Any]:
        return self.request_json("GET", f"/api/v1/submissions/{submission_id}", authenticated=False)

    def retry_json(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        last_error: AgentSubmissionError | None = None
        for attempt in range(3):
            try:
                return self.request_json(method, path, *args, **kwargs)
            except AgentSubmissionError as error:
                last_error = error
                transient = "API is unavailable" in str(error) or bool(
                    re.search(r"API rejected the request \(5\d\d\)", str(error))
                )
                if attempt == 2 or not transient:
                    raise
                time.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    def wait(
        self,
        submission_id: str,
        *,
        poll_seconds: float = 5,
        timeout_seconds: float = 3600,
        progress: Callable[[dict[str, Any]], None] = lambda _record: None,
    ) -> dict[str, Any]:
        last_marker: tuple[Any, ...] | None = None
        deadline = time.monotonic() + timeout_seconds
        while True:
            record = self.submission(submission_id)
            current = record.get("progress") or {}
            marker = (
                record.get("status"),
                current.get("stage"),
                current.get("completed"),
                current.get("message"),
            )
            if marker != last_marker:
                progress(record)
                last_marker = marker
            if record.get("status") in TERMINAL_STATUSES:
                return record
            if time.monotonic() >= deadline:
                raise AgentSubmissionError(
                    f"submission did not finish within {timeout_seconds:.0f}s; "
                    f"follow it at {result_url(self.api_url, submission_id)}"
                )
            time.sleep(max(1, poll_seconds))

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        timeout: int = 30,
    ) -> dict[str, Any]:
        if body is not None and raw is not None:
            raise ValueError("body and raw are mutually exclusive")
        payload = raw
        request_headers = {"user-agent": "cvbench-agent/1", **(headers or {})}
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            request_headers["content-type"] = "application/json"
        if authenticated:
            request_headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=payload,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as error:
            content = error.read()
            try:
                detail = json.loads(content).get("error", {}).get("message")
            except json.JSONDecodeError:
                detail = content.decode(errors="replace")[:1000]
            raise AgentSubmissionError(f"CVBench API rejected the request ({error.code}): {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise AgentSubmissionError(f"CVBench API is unavailable: {error}") from error


def submit_project(
    project: AgentProject,
    client: ControlPlaneClient,
    *,
    wait: bool,
    progress: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    image = build_project(project, progress=progress)
    progress(f"Built {image.image_id}; packaging an immutable upload.")
    with tempfile.TemporaryDirectory(prefix="cvbench-agent-") as directory:
        archive = archive_image(image, Path(directory))
        progress(f"Uploading {format_bytes(archive.size)} directly to CVBench.")
        artifact = client.create_artifact(archive, image.image_id)
        artifact = client.upload_artifact(
            artifact,
            archive,
            progress=lambda sent, total: progress(f"Uploaded {format_bytes(sent)} of {format_bytes(total)}."),
        )
    request_identity = json.dumps(
        {
            "archive_sha256": archive.sha256,
            "image_id": image.image_id,
            "argv": image.command,
            "name": project.name,
            "version": project.version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_key = f"agent-{hashlib.sha256(request_identity.encode()).hexdigest()}"
    submission = client.create_submission(artifact["id"], project, image.command, idempotency_key)
    progress(f"Queued {submission['id']}: {result_url(client.api_url, submission['id'])}")
    if not wait:
        return agent_result(submission, client.api_url)
    completed = client.wait(
        submission["id"],
        progress=lambda record: progress(progress_line(record)),
    )
    return agent_result(completed, client.api_url)


def agent_result(record: dict[str, Any], api_url: str) -> dict[str, Any]:
    result = record.get("result") or {}
    return {
        "schema_version": "cvbench.agent-result/v1",
        "submission_id": record.get("id"),
        "status": record.get("status"),
        "result_url": result_url(api_url, record.get("id", "")),
        "progress": record.get("progress"),
        "scores": result.get("scores"),
        "feedback": record.get("agent_feedback") or result.get("agent_feedback"),
        "findings": result.get("findings", []),
        "error": record.get("error"),
        "record": record,
    }


def progress_line(record: dict[str, Any]) -> str:
    progress = record.get("progress") or {}
    completed = progress.get("completed", 0)
    total = progress.get("total", 0)
    message = progress.get("message") or record.get("status", "unknown")
    return f"{progress.get('stage', record.get('status'))}: {message} ({completed}/{total})"


def result_url(api_url: str, submission_id: str) -> str:
    return f"{normalize_api_url(api_url)}/results/?submission={urllib.parse.quote(submission_id)}"


def normalize_api_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise AgentSubmissionError("API URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise AgentSubmissionError("API URL must use HTTPS except for local development")
    return url


def slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return clean[:80] or "model"


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
