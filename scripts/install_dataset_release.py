#!/usr/bin/env python3
"""Install one hash-pinned CVBench dataset release into the ignored data cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def _role(path: str) -> str:
    if path == "dataset.yaml":
        return "dataset_descriptor"
    if path.startswith("licenses/"):
        return "license"
    if path.startswith("schemas/"):
        return "schema"
    if path.endswith("/video.mp4"):
        return "media"
    if path.endswith("/tracks.jsonl"):
        return "truth"
    if path.endswith("/source.json"):
        return "provenance"
    if path.endswith("/review.jsonl"):
        return "review"
    raise ValueError(f"release contains a file with no canonical role: {path}")


def load_lock(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if set(value) != {
        "archive",
        "dataset_id",
        "install_path",
        "schema_version",
        "version",
    }:
        raise ValueError("dataset lock has missing or undeclared fields")
    if value["schema_version"] != "cvbench.dataset-lock/v1":
        raise ValueError("unsupported dataset lock schema")
    dataset_id = value["dataset_id"]
    version = value["version"]
    install_path = value["install_path"]
    archive = value["archive"]
    if not isinstance(dataset_id, str) or not IDENTIFIER.fullmatch(dataset_id):
        raise ValueError("invalid dataset id")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("invalid dataset version")
    if install_path != f"data/datasets/{dataset_id}":
        raise ValueError("dataset install path must be data/datasets/<dataset-id>")
    if not isinstance(archive, dict) or set(archive) != {"bytes", "sha256", "url"}:
        raise ValueError("dataset archive lock has missing or undeclared fields")
    parsed = urllib.parse.urlparse(str(archive["url"]))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("dataset release URL must use HTTPS")
    if (
        not isinstance(archive["bytes"], int)
        or isinstance(archive["bytes"], bool)
        or archive["bytes"] <= 0
        or not isinstance(archive["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
    ):
        raise ValueError("invalid dataset archive size or SHA-256")
    return value


def _archive_path(lock: dict[str, Any], override: Path | None, temporary: Path) -> Path:
    if override is not None:
        return override.resolve()
    destination = temporary / "release.tar.gz"
    request = urllib.request.Request(
        lock["archive"]["url"],
        headers={"User-Agent": "cvbench-benchmark-dataset-installer/1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    return destination


def _validated_members(
    archive: tarfile.TarFile,
    dataset_id: str,
    version: str,
) -> tuple[str, list[tarfile.TarInfo]]:
    top_level = f"{dataset_id}-{version}"
    members = archive.getmembers()
    if not members:
        raise ValueError("dataset release archive is empty")
    seen: set[str] = set()
    regular: list[tarfile.TarInfo] = []
    for member in members:
        pure = PurePosixPath(member.name)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != top_level:
            raise ValueError(f"unsafe or unexpected archive path: {member.name}")
        normalized = pure.as_posix()
        if normalized in seen:
            raise ValueError(f"duplicate archive path: {member.name}")
        seen.add(normalized)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"dataset archive contains a link or special file: {member.name}")
        if member.isfile():
            regular.append(member)
        elif not member.isdir():
            raise ValueError(f"dataset archive contains an unsupported member: {member.name}")
    required = {
        f"{top_level}/dataset.yaml",
        f"{top_level}/release-manifest.json",
    }
    paths = {member.name for member in regular}
    if not required <= paths:
        raise ValueError("dataset release is missing dataset.yaml or release-manifest.json")
    if not any(path.startswith(f"{top_level}/schemas/") for path in paths):
        raise ValueError("dataset release is missing schemas")
    if not any(path.startswith(f"{top_level}/licenses/") for path in paths):
        raise ValueError("dataset release is missing licenses")
    return top_level, regular


def _extract(archive_path: Path, temporary: Path, lock: dict[str, Any]) -> Path:
    extracted = temporary / "extracted"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        top_level, members = _validated_members(
            archive,
            lock["dataset_id"],
            lock["version"],
        )
        for member in members:
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name}")
            relative = PurePosixPath(member.name).relative_to(top_level)
            destination = extracted.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    return extracted


def _verify_release(root: Path, lock: dict[str, Any]) -> None:
    manifest = json.loads((root / "release-manifest.json").read_text())
    if set(manifest) != {
        "clips",
        "dataset",
        "files",
        "hash_algorithm",
        "manifest_content_sha256",
        "schema_version",
    }:
        raise ValueError("release manifest has missing or undeclared fields")
    supplied_manifest_hash = manifest["manifest_content_sha256"]
    unsigned_manifest = dict(manifest)
    del unsigned_manifest["manifest_content_sha256"]
    if (
        manifest["schema_version"] != "cvbench.dataset-release/v1"
        or manifest["hash_algorithm"] != "sha256"
        or not isinstance(supplied_manifest_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_manifest_hash)
        or supplied_manifest_hash != _canonical_hash(unsigned_manifest)
    ):
        raise ValueError("release manifest content hash or hash algorithm is invalid")
    dataset = manifest["dataset"]
    if (
        not isinstance(dataset, dict)
        or set(dataset)
        != {
            "annotation_scope",
            "certified_at",
            "data_role",
            "evaluation_eligible",
            "id",
            "state",
            "version",
        }
        or dataset["id"] != lock["dataset_id"]
        or dataset["version"] != lock["version"]
        or dataset["state"] != "certified"
        or dataset["data_role"] != "benchmark_truth"
        or dataset["annotation_scope"] != "exhaustive_visible"
        or dataset["evaluation_eligible"] is not True
        or not isinstance(dataset["certified_at"], str)
        or not dataset["certified_at"]
    ):
        raise ValueError("release manifest is not the locked certified dataset")
    declarations = manifest["files"]
    if not isinstance(declarations, list):
        raise ValueError("release manifest files must be a list")
    declared: dict[str, tuple[int, str, str]] = {}
    for item in declarations:
        if not isinstance(item, dict) or set(item) != {"bytes", "path", "role", "sha256"}:
            raise ValueError("release manifest file declaration is invalid")
        path = item["path"]
        pure = PurePosixPath(path) if isinstance(path, str) else PurePosixPath()
        if (
            not path
            or pure.is_absolute()
            or ".." in pure.parts
            or path == "release-manifest.json"
            or path in declared
            or not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] < 0
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or item["role"] != _role(path)
        ):
            raise ValueError("release manifest contains an unsafe or malformed file declaration")
        declared[path] = (item["bytes"], item["sha256"], item["role"])
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {"release-manifest.json", ".cvbench-dataset-lock.json"}
    }
    if set(declared) != actual:
        raise ValueError("release manifest does not declare the exact extracted file set")
    for relative, (expected_bytes, expected_sha256, _) in declared.items():
        path = root / relative
        if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha256:
            raise ValueError(f"release file hash or size mismatch: {relative}")
    clips = manifest["clips"]
    if not isinstance(clips, list) or not clips:
        raise ValueError("release manifest has no clips")
    declared_clip_paths: set[str] = set()
    for clip in clips:
        expected_fields = {
            "annotation_origins",
            "annotation_rows",
            "approved_reviewers",
            "id",
            "path",
            "review_sha256",
            "source_sha256",
            "tracks_sha256",
            "video_sha256",
        }
        if not isinstance(clip, dict) or set(clip) != expected_fields:
            raise ValueError("release manifest clip declaration is invalid")
        clip_id = clip["id"]
        clip_path = clip["path"]
        if (
            not isinstance(clip_id, str)
            or not IDENTIFIER.fullmatch(clip_id)
            or clip_path != f"clips/{clip_id}"
            or clip_path in declared_clip_paths
            or not isinstance(clip["annotation_rows"], int)
            or isinstance(clip["annotation_rows"], bool)
            or clip["annotation_rows"] < 0
            or not isinstance(clip["annotation_origins"], dict)
            or not all(
                isinstance(origin, str)
                and origin
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
                for origin, count in clip["annotation_origins"].items()
            )
            or not isinstance(clip["approved_reviewers"], list)
            or len(clip["approved_reviewers"]) < 2
            or len(set(clip["approved_reviewers"])) != len(clip["approved_reviewers"])
            or not all(isinstance(reviewer, str) and reviewer for reviewer in clip["approved_reviewers"])
        ):
            raise ValueError("release manifest clip identity or certification evidence is invalid")
        declared_clip_paths.add(clip_path)
        for filename, hash_field in (
            ("video.mp4", "video_sha256"),
            ("tracks.jsonl", "tracks_sha256"),
            ("source.json", "source_sha256"),
            ("review.jsonl", "review_sha256"),
        ):
            relative = f"{clip_path}/{filename}"
            if relative not in declared or clip[hash_field] != declared[relative][1]:
                raise ValueError(f"release manifest clip hash does not match files: {clip_id}")
        truth_rows = [
            json.loads(line)
            for line in (root / clip_path / "tracks.jsonl").read_text().splitlines()
            if line
        ]
        origins = Counter(
            row.get("label_origin", {}).get("kind")
            for row in truth_rows
            if isinstance(row, dict)
        )
        if len(truth_rows) != clip["annotation_rows"] or dict(origins) != clip["annotation_origins"]:
            raise ValueError(f"release manifest annotation counts do not match truth: {clip_id}")
        reviews = [
            json.loads(line)
            for line in (root / clip_path / "review.jsonl").read_text().splitlines()
            if line
        ]
        approved_reviewers = {
            review.get("reviewer", {}).get("id")
            for review in reviews
            if isinstance(review, dict)
            and review.get("clip_id") == clip_id
            and review.get("decision") == "approve"
            and review.get("scope") == "all_annotations"
            and review.get("reviewer", {}).get("independent") is True
            and review.get("artifacts")
            == {
                "source_sha256": clip["source_sha256"],
                "tracks_sha256": clip["tracks_sha256"],
                "video_sha256": clip["video_sha256"],
            }
        }
        if approved_reviewers != set(clip["approved_reviewers"]):
            raise ValueError(f"release manifest approved reviewers do not match review evidence: {clip_id}")
    actual_clip_paths = {
        path.relative_to(root).as_posix()
        for path in (root / "clips").iterdir()
        if path.is_dir()
    } if (root / "clips").is_dir() else set()
    if declared_clip_paths != actual_clip_paths:
        raise ValueError("release manifest does not declare the exact clip set")


def install(
    repo_root: Path,
    lock_path: Path,
    *,
    archive_override: Path | None = None,
    replace: bool = False,
) -> Path:
    repo_root = repo_root.resolve()
    lock = load_lock(lock_path.resolve())
    destination = (repo_root / lock["install_path"]).resolve()
    data_root = (repo_root / "data" / "datasets").resolve()
    if destination.parent != data_root:
        raise ValueError("resolved dataset destination escapes data/datasets")
    with tempfile.TemporaryDirectory(prefix="cvbench-dataset-") as temporary_name:
        temporary = Path(temporary_name)
        archive_path = _archive_path(lock, archive_override, temporary)
        expected = lock["archive"]
        if archive_path.stat().st_size != expected["bytes"] or _sha256(archive_path) != expected["sha256"]:
            raise ValueError("dataset release archive hash or size mismatch")
        extracted = _extract(archive_path, temporary, lock)
        _verify_release(extracted, lock)
        (extracted / ".cvbench-dataset-lock.json").write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not replace:
                raise FileExistsError(f"dataset is already installed: {destination}")
            shutil.rmtree(destination)
        os.replace(extracted, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--archive", type=Path, help="Use a local release archive; the lock hash still applies")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(install(args.repo_root, args.lock, archive_override=args.archive, replace=args.replace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
