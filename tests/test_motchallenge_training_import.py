from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from cvbench.scenario import load_scenario
from scripts import import_motchallenge_training as importer


def _jpeg(width: int, height: int, value: int) -> bytes:
    image = np.full((height, width, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _seqinfo(name: str, *, fps: int, frames: int, width: int = 8, height: int = 6) -> bytes:
    return (
        "[Sequence]\n"
        f"name={name}\n"
        "imDir=img1\n"
        f"frameRate={fps}\n"
        f"seqLength={frames}\n"
        f"imWidth={width}\n"
        f"imHeight={height}\n"
        "imExt=.jpg\n"
    ).encode()


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in members.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value)


def _requirements(ingest: Path) -> dict[str, importer.ArchiveRequirement]:
    return {
        path.name: importer.ArchiveRequirement(
            bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            url=f"https://motchallenge.net/data/{path.name}",
            accepted_official_bytes_utc="synthetic-fixture",
        )
        for path in sorted(ingest.glob("*.zip"))
    }


@pytest.fixture
def synthetic_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    mot17_gt = (
        b"1,1,1,1,4,4,1,1,1\n"
        b"2,1,0,1,4,4,1,1,0.5\n"
        b"1,2,2,2,3,3,1,8,0.25\n"
        b"2,2,2,2,3,3,1,8,0\n"
        b"1,3,3,3,2,2,0,13,1\n"
    )
    mot17_info = _seqinfo("MOT17-02", fps=30, frames=2)
    _write_zip(
        ingest / "MOT16.zip",
        {
            "train/MOT16-02/seqinfo.ini": _seqinfo("MOT16-02", fps=30, frames=2),
            "train/MOT16-02/img1/000001.jpg": _jpeg(8, 6, 10),
            "train/MOT16-02/img1/000002.jpg": _jpeg(8, 6, 20),
        },
    )
    mot17_members = {}
    for variant in importer.MOT17_VARIANTS:
        root = f"train/MOT17-02-{variant}"
        mot17_members[f"{root}/seqinfo.ini"] = mot17_info
        mot17_members[f"{root}/gt/gt.txt"] = mot17_gt
    _write_zip(ingest / "MOT17Labels.zip", mot17_members)
    _write_zip(
        ingest / "MOT20.zip",
        {
            "MOT20/train/MOT20-01/seqinfo.ini": _seqinfo("MOT20-01", fps=25, frames=1),
            "MOT20/train/MOT20-01/img1/000001.jpg": _jpeg(8, 6, 30),
            "MOT20/train/MOT20-01/gt/gt.txt": b"1,7,4,2,3,3,1,2,0.75\n",
        },
    )
    monkeypatch.setattr(importer, "ARCHIVES", _requirements(ingest))
    monkeypatch.setattr(
        importer,
        "SEQUENCES",
        (
            importer.SequenceSpec(
                sequence_id="MOT17-02",
                pixel_archive="MOT16.zip",
                pixel_root="train/MOT16-02",
                annotation_archive="MOT17Labels.zip",
                gt_members=tuple(
                    f"train/MOT17-02-{variant}/gt/gt.txt" for variant in importer.MOT17_VARIANTS
                ),
                seqinfo_members=tuple(
                    f"train/MOT17-02-{variant}/seqinfo.ini" for variant in importer.MOT17_VARIANTS
                ),
                detector_variants=importer.MOT17_VARIANTS,
            ),
            importer.SequenceSpec(
                sequence_id="MOT20-01",
                pixel_archive="MOT20.zip",
                pixel_root="MOT20/train/MOT20-01",
                annotation_archive="MOT20.zip",
                gt_members=("MOT20/train/MOT20-01/gt/gt.txt",),
                seqinfo_members=("MOT20/train/MOT20-01/seqinfo.ini",),
                detector_variants=(),
            ),
        ),
    )
    return ingest


def _rows(output: Path, sequence: str) -> list[dict]:
    path = output / "sequences" / sequence / "ground_truth.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_production_selection_is_11_unique_training_videos() -> None:
    assert len(importer.SEQUENCES) == 11
    assert len({spec.sequence_id for spec in importer.SEQUENCES}) == 11
    mot17 = [spec for spec in importer.SEQUENCES if spec.sequence_id.startswith("MOT17")]
    mot20 = [spec for spec in importer.SEQUENCES if spec.sequence_id.startswith("MOT20")]
    assert len(mot17) == 7
    assert len(mot20) == 4
    assert all(spec.pixel_archive == "MOT16.zip" for spec in mot17)
    assert all(spec.detector_variants == ("DPM", "FRCNN", "SDP") for spec in mot17)


def test_import_is_deterministic_deduplicated_and_current_contract(
    synthetic_archives: Path, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = importer.import_corpus(synthetic_archives, first)
    second_manifest = importer.import_corpus(synthetic_archives, second)

    assert (first / "artifacts.sha256").read_bytes() == (second / "artifacts.sha256").read_bytes()
    assert first_manifest == second_manifest
    assert first_manifest["sequence_count"] == 2
    mot17 = first_manifest["sequences"]["mot17-02"]
    assert mot17["detector_variants_collapsed"] == ["DPM", "FRCNN", "SDP"]
    assert mot17["detector_gt_copies"] == 3
    assert mot17["detector_gt_copies_byte_identical"] is True
    assert len(list((first / "sequences/mot17-02/frames").glob("*.jpg"))) == 2

    scenario = yaml.safe_load((first / "sequences/mot17-02/scenario.yaml").read_text())
    assert [frame["source_timestamp_ns"] for frame in scenario["frames"]] == [0, 33_333_333]
    loaded = load_scenario(first / "sequences/mot17-02/scenario.yaml")
    assert len(loaded.frames) == 2
    assert len(loaded.ground_truth) == 5
    importer.verify_output(first)


def test_mapping_preserves_boxes_identities_classes_visibility_and_ignore_semantics(
    synthetic_archives: Path, tmp_path: Path
) -> None:
    output = tmp_path / "corpus"
    importer.import_corpus(synthetic_archives, output)
    rows = _rows(output, "mot17-02")

    person = rows[0]
    assert person["target_id"] == "MOT17-02:mot-id:000001"
    assert person["class_id"] == "person"
    assert person["source_mot"]["bbox_xywh"] == [1, 1, 4, 4]
    assert person["source_mot"]["bbox_xyxy_unclipped"] == [0, 0, 4, 4]
    assert person["bbox_xyxy"] == [0, 0, 4, 4]
    assert person["visibility_fraction"] == 1
    assert person["occlusion"] == "none"
    assert person["evaluation_state"] == "score"
    assert person["eligible_for_detection"] is True
    assert person["ignore"] is False

    truncated_person = next(
        row for row in rows if row["source_timestamp_ns"] == 33_333_333 and row["class_id"] == "person"
    )
    assert truncated_person["target_id"] == person["target_id"]
    assert truncated_person["source_mot"]["bbox_xyxy_unclipped"] == [-1, 0, 3, 4]
    assert truncated_person["bbox_xyxy"] == [0, 0, 3, 4]
    assert truncated_person["truncated"] is True
    assert truncated_person["visibility_fraction"] == 0.5
    assert truncated_person["occlusion"] == "partial"

    distractor = next(row for row in rows if row["class_id"] == "distractor")
    assert distractor["distractor"] is True
    assert distractor["evaluation_state"] == "ignore"
    assert distractor["ignore"] is True
    assert distractor["ignore_region"] is False

    crowd = next(row for row in rows if row["class_id"] == "crowd")
    assert crowd["source_mot"]["mark"] == 0
    assert crowd["ignore"] is True
    assert crowd["ignore_region"] is True
    assert crowd["ignore_region_id"] == crowd["target_id"]

    mot20 = _rows(output, "mot20-01")[0]
    assert mot20["class_id"] == "person_on_vehicle"
    assert mot20["source_mot"]["class_id"] == 2
    assert mot20["visibility_fraction"] == 0.75
    assert mot20["occlusion"] == "partial"


def test_missing_or_changed_archive_fails_closed(
    synthetic_archives: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mot20 = synthetic_archives / "MOT20.zip"
    mot20_bytes = mot20.read_bytes()
    mot20.unlink()
    with pytest.raises(RuntimeError, match="missing pinned official archive"):
        importer.import_corpus(synthetic_archives, tmp_path / "missing")
    mot20.write_bytes(mot20_bytes)

    bad = dict(importer.ARCHIVES)
    requirement = bad["MOT16.zip"]
    bad["MOT16.zip"] = importer.ArchiveRequirement(
        bytes=requirement.bytes,
        sha256="0" * 64,
        url=requirement.url,
        accepted_official_bytes_utc=requirement.accepted_official_bytes_utc,
    )
    monkeypatch.setattr(importer, "ARCHIVES", bad)
    with pytest.raises(RuntimeError, match="SHA-256 drift"):
        importer.import_corpus(synthetic_archives, tmp_path / "changed")


def test_variant_disagreement_and_output_tampering_fail_closed(
    synthetic_archives: Path, tmp_path: Path
) -> None:
    labels = synthetic_archives / "MOT17Labels.zip"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(labels, "a", compression=zipfile.ZIP_STORED) as archive,
    ):
        archive.writestr("train/MOT17-02-SDP/gt/gt.txt", b"1,1,1,1,1,1,1,1,1\n")
    importer.ARCHIVES["MOT17Labels.zip"] = _requirements(synthetic_archives)["MOT17Labels.zip"]
    with pytest.raises(RuntimeError, match="duplicate member"):
        importer.import_corpus(synthetic_archives, tmp_path / "variant-drift")

    clean_ingest = tmp_path / "clean-ingest"
    clean_ingest.mkdir()
    for source in synthetic_archives.glob("*.zip"):
        if source.name != "MOT17Labels.zip":
            (clean_ingest / source.name).write_bytes(source.read_bytes())
    with zipfile.ZipFile(labels) as source:
        members = {}
        for info in source.infolist():
            if info.filename not in members:
                members[info.filename] = source.read(info)
    members["train/MOT17-02-SDP/gt/gt.txt"] = b"1,1,1,1,1,1,1,1,1\n"
    _write_zip(clean_ingest / "MOT17Labels.zip", members)
    importer.ARCHIVES = _requirements(clean_ingest)
    with pytest.raises(RuntimeError, match="detector GT copies are not byte-identical"):
        importer.import_corpus(clean_ingest, tmp_path / "variant-drift-clean")


def test_output_hash_verification_detects_tampering(synthetic_archives: Path, tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    importer.import_corpus(synthetic_archives, output)
    frame = output / "sequences/mot20-01/frames/000001.jpg"
    frame.write_bytes(frame.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="artifact hash mismatch"):
        importer.verify_output(output)


def test_output_verification_rejects_symlink(synthetic_archives: Path, tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    importer.import_corpus(synthetic_archives, output)
    frame = output / "sequences/mot20-01/frames/000001.jpg"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(frame.read_bytes())
    frame.unlink()
    frame.symlink_to(outside)
    with pytest.raises(RuntimeError, match="contains symlink"):
        importer.verify_output(output)


def test_unsafe_archive_member_fails_closed(synthetic_archives: Path, tmp_path: Path) -> None:
    mot20 = synthetic_archives / "MOT20.zip"
    with zipfile.ZipFile(mot20, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("../escape", b"not allowed")
    importer.ARCHIVES["MOT20.zip"] = _requirements(synthetic_archives)["MOT20.zip"]
    with pytest.raises(RuntimeError, match="unsafe ZIP member path"):
        importer.import_corpus(synthetic_archives, tmp_path / "unsafe")
