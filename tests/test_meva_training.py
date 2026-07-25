import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from cvbench.errors import ConfigurationError
from cvbench.scenario import load_scenario
from scripts import prepare_meva_training


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    ingest = tmp_path / ".local-ingest" / "meva"
    videos = ingest / "videos"
    repo = ingest / "annotations" / "meva-data-repo"
    annotation_dir = (
        repo
        / "annotation"
        / "DIVA-phase-2"
        / "MEVA"
        / "kitware-meva-training"
        / "2018-03-05"
        / "13"
    )
    videos.mkdir(parents=True)
    annotation_dir.mkdir(parents=True)

    stem = "2018-03-05.13-15-00.13-20-00.bus.G340"
    video = videos / f"{stem}.r13.avi"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (8, 6))
    assert writer.isOpened()
    for index in range(4):
        writer.write(np.full((6, 8, 3), index * 40, dtype=np.uint8))
    writer.release()

    geom = annotation_dir / f"{stem}.geom.yml"
    geom.write_text(
        "- { geom: { id1: 7, ts0: 0, g0: '1 1 5 5' } }\n"
        "- { geom: { id1: 7, ts0: 2, g0: '1 1 6 5' } }\n"
        "- { geom: { id1: 8, ts0: 2, g0: '2 1 7 5' } }\n"
    )
    types = annotation_dir / f"{stem}.types.yml"
    types.write_text(
        "- { types: { id1: 7, cset3: { Person: 1.0 } } }\n"
        "- { types: { id1: 8, cset3: { Vehicle: 1.0 } } }\n"
    )

    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Synthetic Fixture")
    _git(repo, "remote", "add", "origin", "https://gitlab.kitware.com/meva/meva-data-repo.git")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "synthetic MEVA training fixture")

    license_path = ingest / "MEVA-data-license.txt"
    license_path.write_text("synthetic official-license fixture\n")
    monkeypatch.setattr(
        prepare_meva_training,
        "MEVA_LICENSE_SHA256",
        hashlib.sha256(license_path.read_bytes()).hexdigest(),
    )
    return {
        "ingest": ingest,
        "video": video,
        "geom": geom,
        "types": types,
        "license": license_path,
    }


def _prepare(paths: dict[str, Path], output_name: str = "meva-sample") -> Path:
    return prepare_meva_training.prepare_sequence(
        ingest_root=paths["ingest"],
        scenario_id="meva-sample",
        video_path=paths["video"],
        geom_path=paths["geom"],
        types_path=paths["types"],
        license_path=paths["license"],
        output=paths["ingest"] / "prepared" / output_name,
    )


def test_import_preserves_positive_annotations_provenance_and_source_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    output = _prepare(paths)

    manifest = yaml.safe_load((output / "scenario.yaml").read_text())
    rows = [json.loads(line) for line in (output / "ground_truth.jsonl").read_text().splitlines()]
    provenance = json.loads((output / "provenance.json").read_text())

    assert manifest["data_role"] == "model_training_only"
    assert manifest["evaluation_eligible"] is False
    assert manifest["annotation_policy"] == {
        "scope": "activity_bounded_positive_only",
        "exhaustive": False,
        "missing_annotation_semantics": "unknown_not_background",
    }
    assert manifest["ontology"] == ["Person", "Vehicle"]
    assert [frame["source_frame_index"] for frame in manifest["frames"]] == [0, 2]
    assert [frame["source_timestamp_ns"] for frame in manifest["frames"]] == [0, 66_666_667]
    assert {row["target_id"] for row in rows} == {"7", "8"}
    assert {row["source_track_id"] for row in rows} == {7, 8}
    assert [row["bbox_xyxy"] for row in rows] == [[1, 1, 5, 5], [1, 1, 6, 5], [2, 1, 7, 5]]
    assert all(row["training_only"] and row["visibility_fraction"] is None for row in rows)
    assert all(row["occlusion"] == "unknown" for row in rows)
    assert provenance["source_video"]["sha256"] == hashlib.sha256(paths["video"].read_bytes()).hexdigest()
    assert provenance["source_annotations"]["commit"]
    assert provenance["license"]["id"] == "CC-BY-4.0"
    assert prepare_meva_training.verify_prepared(output)


def test_import_is_byte_deterministic_and_training_manifest_is_not_scoreable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    first = _prepare(paths, "first")
    second = _prepare(paths, "second")
    first_files = {
        path.relative_to(first).as_posix(): path.read_bytes() for path in first.rglob("*") if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path.read_bytes() for path in second.rglob("*") if path.is_file()
    }
    assert first_files == second_files
    with pytest.raises(ConfigurationError, match="model training data"):
        load_scenario(first / "scenario.yaml")


def test_import_rejects_modified_annotations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    paths["geom"].write_text(paths["geom"].read_text() + "# local edit\n")
    with pytest.raises(RuntimeError, match="not clean"):
        _prepare(paths)


def test_import_rejects_unpinned_license_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    paths["license"].write_text("changed terms\n")
    with pytest.raises(RuntimeError, match="pinned official CC BY 4.0"):
        _prepare(paths)
