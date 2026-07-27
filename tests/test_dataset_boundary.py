from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from scripts.install_dataset_release import install, load_lock
from scripts.materialize_dataset_release import materialize

ROOT = Path(__file__).resolve().parents[1]


def _canonical_hash(value: object) -> str:
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def _role(path: str) -> str:
    if path == "dataset.yaml":
        return "dataset_descriptor"
    if path.startswith("licenses/"):
        return "license"
    if path.startswith("schemas/"):
        return "schema"
    return {
        "video.mp4": "media",
        "tracks.jsonl": "truth",
        "source.json": "provenance",
        "review.jsonl": "review",
    }[path.rsplit("/", 1)[-1]]


def _release(
    tmp_path: Path,
    *,
    symlink: bool = False,
    state: str = "certified",
    valid_manifest_hash: bool = True,
    data_role: str = "benchmark_truth",
    annotation_scope: str = "exhaustive_visible",
    evaluation_eligible: bool = True,
) -> tuple[Path, Path]:
    dataset_id = "example-dataset"
    version = "1.2.3"
    top = f"{dataset_id}-{version}"
    files = {
        "dataset.yaml": (
            b"schema_version: cvbench.dataset/v1\n"
            b"id: example-dataset\n"
            b"version: 1.2.3\n"
            b"title: Example dataset\n"
            b"description: Installer and materializer fixture.\n"
            + f"state: {state}\n".encode()
            + f"data_role: {data_role}\n".encode()
            + f"annotation_scope: {annotation_scope}\n".encode()
            + f"evaluation_eligible: {str(evaluation_eligible).lower()}\n".encode()
            + b"ontology:\n"
            b"  classes:\n"
            b"    - id: person\n"
            b"      description: Person.\n"
            b"clips:\n"
            b"  - id: clip-one\n"
            b"    path: clips/clip-one\n"
            b"certification:\n"
            b"  policy: cvbench.dataset-certification/v1\n"
            b"  required_independent_approvals: 2\n"
            b"  certified_at: '2026-07-27T00:00:00Z'\n"
        ),
        "clips/clip-one/video.mp4": b"video",
        "clips/clip-one/tracks.jsonl": (
            b'{"schema_version":"cvbench.track-annotation/v1","clip_id":"clip-one",'
            b'"frame_index":0,"source_timestamp_ns":0,"track_id":"person-1",'
            b'"class_id":"person","bbox_xyxy":[1,2,8,12],"occlusion":"none",'
            b'"truncated":false,"label_origin":{"kind":"human","model_run_ids":[]}}\n'
            b'{"schema_version":"cvbench.track-annotation/v1","clip_id":"clip-one",'
            b'"frame_index":1,"source_timestamp_ns":500000000,"track_id":"person-1",'
            b'"class_id":"person","bbox_xyxy":[2,2,9,12],"occlusion":"none",'
            b'"truncated":false,"label_origin":{"kind":"human","model_run_ids":[]}}\n'
        ),
        "clips/clip-one/source.json": (
            b'{"schema_version":"cvbench.source/v1","clip_id":"clip-one",'
            b'"source":{"title":"Fixture","uri":"synthetic://fixture",'
            b'"sha256":"0000000000000000000000000000000000000000000000000000000000000000",'
            b'"license":{"spdx":"MIT","name":"MIT","url":"https://example.invalid/license",'
            b'"file":"licenses/LICENSE.txt"}},"media":{"width":16,"height":16,'
            b'"frame_count":2,"fps_numerator":2,"fps_denominator":1},'
            b'"transformations":[],"model_runs":[]}\n'
        ),
        "clips/clip-one/review.jsonl": b"",
        "schemas/tracks.schema.json": b"{}\n",
        "licenses/LICENSE.txt": b"fixture\n",
    }
    review_artifacts = {
        "video_sha256": hashlib.sha256(files["clips/clip-one/video.mp4"]).hexdigest(),
        "tracks_sha256": hashlib.sha256(files["clips/clip-one/tracks.jsonl"]).hexdigest(),
        "source_sha256": hashlib.sha256(files["clips/clip-one/source.json"]).hexdigest(),
    }
    files["clips/clip-one/review.jsonl"] = "".join(
        json.dumps(
            {
                "schema_version": "cvbench.review/v1",
                "review_id": f"review-{reviewer}",
                "clip_id": "clip-one",
                "reviewed_at": "2026-07-27T00:00:00Z",
                "reviewer": {
                    "id": reviewer,
                    "kind": "human",
                    "independent": True,
                },
                "decision": "approve",
                "scope": "all_annotations",
                "artifacts": review_artifacts,
                "rationale": "Complete fixture review.",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for reviewer in ("reviewer-a", "reviewer-b")
    ).encode()
    manifest: dict[str, object] = {
        "schema_version": "cvbench.dataset-release/v1",
        "hash_algorithm": "sha256",
        "dataset": {
            "id": dataset_id,
            "version": version,
            "state": state,
            "data_role": data_role,
            "annotation_scope": annotation_scope,
            "evaluation_eligible": evaluation_eligible,
            "certified_at": "2026-07-27T00:00:00Z",
        },
        "files": [
            {
                "path": path,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "role": _role(path),
            }
            for path, body in sorted(files.items())
        ],
        "clips": [
            {
                "id": "clip-one",
                "path": "clips/clip-one",
                "video_sha256": hashlib.sha256(files["clips/clip-one/video.mp4"]).hexdigest(),
                "tracks_sha256": hashlib.sha256(files["clips/clip-one/tracks.jsonl"]).hexdigest(),
                "source_sha256": hashlib.sha256(files["clips/clip-one/source.json"]).hexdigest(),
                "review_sha256": hashlib.sha256(files["clips/clip-one/review.jsonl"]).hexdigest(),
                "annotation_rows": 2,
                "annotation_origins": {"human": 2},
                "approved_reviewers": ["reviewer-a", "reviewer-b"],
            }
        ],
    }
    manifest["manifest_content_sha256"] = (
        _canonical_hash(manifest) if valid_manifest_hash else "0" * 64
    )
    files["release-manifest.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as handle:
        for path, body in sorted(files.items()):
            member = tarfile.TarInfo(f"{top}/{path}")
            member.size = len(body)
            member.mtime = 0
            handle.addfile(member, io.BytesIO(body))
        if symlink:
            member = tarfile.TarInfo(f"{top}/clips/clip-one/escape")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../../../secret"
            handle.addfile(member)
    lock = {
        "schema_version": "cvbench.dataset-lock/v1",
        "dataset_id": dataset_id,
        "version": version,
        "install_path": f"data/datasets/{dataset_id}",
        "archive": {
            "url": "https://example.invalid/example-dataset-1.2.3.tar.gz",
            "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
    }
    lock_path = tmp_path / "dataset.lock.json"
    lock_path.write_text(json.dumps(lock))
    return archive, lock_path


def test_installer_accepts_a_hash_pinned_release(tmp_path: Path) -> None:
    archive, lock = _release(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    installed = install(repo, lock, archive_override=archive)

    assert installed == repo / "data/datasets/example-dataset"
    assert (installed / "clips/clip-one/video.mp4").read_bytes() == b"video"
    assert json.loads((installed / ".cvbench-dataset-lock.json").read_text()) == load_lock(lock)


def test_materializer_converts_only_explicit_clips_without_editing_truth(
    tmp_path: Path,
) -> None:
    archive, lock = _release(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    installed = install(repo, lock, archive_override=archive)
    installed_truth_before = (installed / "clips/clip-one/tracks.jsonl").read_bytes()

    destination = materialize(
        repo,
        lock,
        ["clip-one"],
        decoder=lambda _path, _media: [b"jpeg-zero", b"jpeg-one"],
    )

    scenario = yaml.safe_load((destination / "clip-one/scenario.yaml").read_text())
    rows = [
        json.loads(line)
        for line in (destination / "clip-one/ground_truth.jsonl").read_text().splitlines()
    ]
    assert scenario["id"] == "example-dataset--clip-one"
    assert [frame["source_timestamp_ns"] for frame in scenario["frames"]] == [0, 500_000_000]
    assert [row["bbox_xyxy"] for row in rows] == [[1, 2, 8, 12], [2, 2, 9, 12]]
    assert rows[0]["entry_event"] is True
    assert rows[1]["exit_event"] is True
    assert (installed / "clips/clip-one/tracks.jsonl").read_bytes() == installed_truth_before
    assert (destination / "materialization.json").is_file()


def test_installer_rejects_archive_hash_mismatch(tmp_path: Path) -> None:
    archive, lock = _release(tmp_path)
    value = json.loads(lock.read_text())
    value["archive"]["sha256"] = "0" * 64
    lock.write_text(json.dumps(value))
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="archive hash or size mismatch"):
        install(repo, lock, archive_override=archive)


def test_installer_rejects_links_before_installing(tmp_path: Path) -> None:
    archive, lock = _release(tmp_path, symlink=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="link or special file"):
        install(repo, lock, archive_override=archive)
    assert not (repo / "data").exists()


@pytest.mark.parametrize(
    ("release_options", "message"),
    [
        ({"state": "draft"}, "not the locked certified dataset"),
        ({"valid_manifest_hash": False}, "content hash"),
        ({"data_role": "training_only"}, "not the locked certified dataset"),
        ({"annotation_scope": "activity_bounded"}, "not the locked certified dataset"),
        ({"evaluation_eligible": False}, "not the locked certified dataset"),
    ],
)
def test_installer_rejects_uncertified_or_self_hash_invalid_release(
    tmp_path: Path,
    release_options: dict[str, object],
    message: str,
) -> None:
    archive, lock = _release(tmp_path, **release_options)
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match=message):
        install(repo, lock, archive_override=archive)


def test_legacy_real_video_is_explicitly_locked_as_compatibility_only() -> None:
    lock = json.loads(
        (ROOT / "datasets/locks/real-video-v2.compatibility.json").read_text()
    )
    fixture = ROOT / lock["fixture_path"]

    assert lock["status"] == "legacy_runtime_compatibility_only"
    assert lock["install_path"] == "data/real-video-v2"
    assert hashlib.sha256((fixture / "archives.json").read_bytes()).hexdigest() == lock[
        "archives_manifest_sha256"
    ]
    assert hashlib.sha256(
        (fixture / "expected-frame-sha256.txt").read_bytes()
    ).hexdigest() == lock["frame_manifest_sha256"]
    assert (fixture / "corpus-fingerprint.txt").read_text().strip() == lock[
        "corpus_fingerprint"
    ]


def test_benchmark_repository_does_not_own_dataset_authoring_workflows() -> None:
    forbidden = {
        "scripts/prepare_real_video.py",
        "scripts/prepare_real_video_container.sh",
        "scripts/reconcile_real_video_truth.py",
        "scripts/verify_real_video_corpus.py",
        "examples/Dockerfile.real-video-prep",
        "requirements-real-video.lock",
    }

    assert all(not (ROOT / path).exists() for path in forbidden)
    assert not (ROOT / "scenarios/real-video-v3").exists()
    public = (ROOT / "benchmarks/public-whole-system-v3.yaml").read_text()
    assert "scenarios/real-video-v2" in public
    assert "real-video-v3" not in public
