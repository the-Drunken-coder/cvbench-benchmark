#!/usr/bin/env python3
"""Hydrate the frozen real-video-v2 compatibility fixture.

New benchmark datasets are installed from pinned releases with
``install_dataset_release.py``. This hydrator exists only to reproduce the
legacy v2 and public-v3 runtime inputs without changing their bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

SCENARIO_IDS = ("rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6")
FRAME_COUNT = 150
COMPATIBILITY_LOCK = Path("datasets/locks/real-video-v2.compatibility.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_compatibility_lock(repo_root: Path) -> dict:
    path = repo_root / COMPATIBILITY_LOCK
    value = json.loads(path.read_text())
    expected_fields = {
        "archives_manifest_sha256",
        "corpus_fingerprint",
        "dataset_id",
        "fixture_path",
        "frame_manifest_sha256",
        "install_path",
        "schema_version",
        "status",
        "version",
    }
    if set(value) != expected_fields:
        raise RuntimeError("real-video compatibility lock has missing or undeclared fields")
    expected_identity = {
        "dataset_id": "real-video-v2",
        "fixture_path": "scenarios/real-video-v2",
        "install_path": "data/real-video-v2",
        "schema_version": "cvbench.dataset-compatibility-lock/v1",
        "status": "legacy_runtime_compatibility_only",
        "version": "2.0.0",
    }
    if any(value[key] != expected for key, expected in expected_identity.items()):
        raise RuntimeError("real-video compatibility lock has the wrong identity")
    for field in (
        "archives_manifest_sha256",
        "corpus_fingerprint",
        "frame_manifest_sha256",
    ):
        if not isinstance(value[field], str) or len(value[field]) != 64:
            raise RuntimeError(f"real-video compatibility lock has an invalid {field}")
    fixture = repo_root / value["fixture_path"]
    if _sha256(fixture / "archives.json") != value["archives_manifest_sha256"]:
        raise RuntimeError("real-video archive manifest does not match its compatibility lock")
    if _sha256(fixture / "expected-frame-sha256.txt") != value["frame_manifest_sha256"]:
        raise RuntimeError("real-video frame manifest does not match its compatibility lock")
    if (fixture / "corpus-fingerprint.txt").read_text().strip() != value["corpus_fingerprint"]:
        raise RuntimeError("real-video corpus fingerprint does not match its compatibility lock")
    return value


def _load_expected(repo_root: Path) -> dict[str, str]:
    manifest = repo_root / "scenarios" / "real-video-v2" / "expected-frame-sha256.txt"
    expected: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        if len(digest) != 64 or relative in expected:
            raise RuntimeError("malformed real-video frame hash manifest")
        expected[relative] = digest
    if len(expected) != len(SCENARIO_IDS) * FRAME_COUNT:
        raise RuntimeError(f"expected {len(SCENARIO_IDS) * FRAME_COUNT} frame hashes, found {len(expected)}")
    return expected


def _load_archives(repo_root: Path) -> dict:
    path = repo_root / "scenarios" / "real-video-v2" / "archives.json"
    value = json.loads(path.read_text())
    if set(value) != {"archives", "frame_count", "schema_version"}:
        raise RuntimeError("real-video archive manifest has undeclared fields")
    if value["schema_version"] != "cvbench.real-video-archives/v1" or value["frame_count"] != 450:
        raise RuntimeError("invalid real-video archive manifest")
    if set(value["archives"]) != set(SCENARIO_IDS):
        raise RuntimeError("real-video archive manifest has the wrong scenario set")
    return value


def _validated_archive(repo_root: Path, declaration: dict, scenario_id: str) -> Path:
    if set(declaration) != {"bytes", "path", "sha256"}:
        raise RuntimeError(f"{scenario_id} archive declaration has undeclared fields")
    expected = f"scenarios/real-video-v2/archives/{scenario_id}.frames.tar"
    if declaration["path"] != expected:
        raise RuntimeError(f"{scenario_id} archive path is not allowlisted")
    unresolved = repo_root / declaration["path"]
    cursor = repo_root
    for part in unresolved.relative_to(repo_root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"{scenario_id} archive path contains a symlink")
    path = unresolved.resolve()
    allowed = (repo_root / "scenarios" / "real-video-v2" / "archives").resolve()
    if path.parent != allowed or not path.is_file():
        raise RuntimeError(f"{scenario_id} archive is not a regular allowlisted file")
    if path.stat().st_size != declaration["bytes"] or _sha256(path) != declaration["sha256"]:
        raise RuntimeError(f"{scenario_id} archive hash or size mismatch")
    return path


def _extract_frames(archive: Path, output: Path, scenario_id: str, expected: dict[str, str]) -> None:
    names = {f"frames/frame-{index:04d}.jpg" for index in range(FRAME_COUNT)}
    with tarfile.open(archive, "r:") as handle:
        members = handle.getmembers()
        if len(members) != FRAME_COUNT or {member.name for member in members} != names:
            raise RuntimeError(f"{scenario_id} archive entries do not match the declared frame set")
        for member in members:
            if not member.isfile() or member.issym() or member.islnk():
                raise RuntimeError(f"{scenario_id} archive contains a non-regular entry")
            source = handle.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read {scenario_id}/{member.name}")
            body = source.read()
            relative = f"{scenario_id}/{member.name}"
            if hashlib.sha256(body).hexdigest() != expected[relative]:
                raise RuntimeError(f"frame hash mismatch for {relative}")
            destination = output / scenario_id / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)


def hydrate(repo_root: Path, output: Path | None = None) -> Path:
    repo_root = repo_root.resolve()
    lock = _load_compatibility_lock(repo_root)
    expected_output = (repo_root / lock["install_path"]).resolve()
    output = (output or expected_output).resolve()
    if output != expected_output:
        raise RuntimeError("hydration output must be the dedicated data/real-video-v2 directory")
    expected = _load_expected(repo_root)
    archive_manifest = _load_archives(repo_root)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    for scenario_id in SCENARIO_IDS:
        declaration = archive_manifest["archives"][scenario_id]["frame_archive"]
        archive = _validated_archive(repo_root, declaration, scenario_id)
        _extract_frames(archive, output, scenario_id, expected)
        source_gt = repo_root / "scenarios" / "real-video-v2" / scenario_id / "ground_truth.jsonl"
        shutil.copyfile(source_gt, output / scenario_id / "ground_truth.jsonl")
    source_manifest = repo_root / "scenarios" / "real-video-v2" / "expected-frame-sha256.txt"
    shutil.copyfile(source_manifest, output / "expected-frame-sha256.txt")
    shutil.copyfile(
        repo_root / "scenarios" / "real-video-v2" / "corpus-fingerprint.txt",
        output / "corpus-fingerprint.txt",
    )
    shutil.copyfile(repo_root / "scenarios" / "real-video-v2" / "archives.json", output / "archives.json")
    entries = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "artifacts.sha256":
            entries.append(f"{_sha256(path)}  {path.relative_to(output).as_posix()}")
    (output / "artifacts.sha256").write_text("\n".join(entries) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(hydrate(args.repo_root, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
