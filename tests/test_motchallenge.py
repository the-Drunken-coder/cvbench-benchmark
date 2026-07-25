from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from cvbench.scenario import load_scenario
from scripts.prepare_motchallenge import (
    ARCHIVES,
    SEQUENCES,
    _audit_zip,
    _normalize_gt,
    _timestamp_ns,
    verify_hydrated,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scenarios" / "motchallenge-v1"
IDS = [
    "mot17-02",
    "mot17-04",
    "mot17-09",
    "mot17-10",
    "mot17-11",
    "mot17-13",
    "mot20-01",
    "mot20-02",
    "mot20-03",
    "mot20-05",
]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_pinned_archive_and_tranche_contract() -> None:
    manifest = json.loads((SOURCE / "ingest-manifest.json").read_text())
    assert manifest["official_archives_only"] is True
    assert manifest["public_detections_used"] is False
    assert manifest["mots_or_mot15_representations_used"] is False
    assert manifest["selected_sequence_ids"] == [spec.sequence_id for spec in SEQUENCES]
    assert list(ARCHIVES) == ["MOT16.zip", "MOT17Labels.zip", "MOT20.zip"]
    for name, expected in ARCHIVES.items():
        audit = manifest["archive_audits"][name]
        assert (audit["bytes"], audit["sha256"], audit["url"]) == (
            expected["bytes"],
            expected["sha256"],
            expected["url"],
        )
        assert audit["zip_crc"] == "verified"
        assert set(audit["path_safety"].values()) == {0}
    assert manifest["totals"] == {
        "boundary_corrections": 26416,
        "duration_seconds": 511.54,
        "frames": 13410,
        "neutral_ignore_boxes": 293614,
        "neutral_ignore_tracks": 344,
        "published_headline_boxes_including_mot20_ignore": 1442300,
        "published_headline_trajectories_including_mot20_ignore": 2745,
        "scored_person_boxes": 1239994,
        "scored_person_tracks": 2628,
        "sequences": 10,
    }
    assert "Original container PTS is unavailable and is not claimed" in manifest["cadence_disclosure"]


def test_scenario_order_cardinality_ontology_and_derived_cadence(tmp_path: Path) -> None:
    manifest = json.loads((SOURCE / "ingest-manifest.json").read_text())
    benchmark = yaml.safe_load((ROOT / "benchmarks" / "motchallenge-v1.yaml").read_text())
    declared = []
    for spec, relative in zip(SEQUENCES, benchmark["scenarios"], strict=True):
        scenario = yaml.safe_load((ROOT / "benchmarks" / relative).resolve().read_text())
        declared.append(scenario["id"])
        info = manifest["sequence_results"][scenario["id"]]
        assert info["fps"] == spec.expected_fps
        assert scenario["ontology"] == ["person"]
        assert scenario["annotation_scope"] == "exhaustive_full_frame_pedestrians_with_neutral_ignore"
        assert len(scenario["frames"]) == info["frame_count"]
        assert [frame["frame_index"] for frame in scenario["frames"]] == list(range(info["frame_count"]))
        assert [frame["source_timestamp_ns"] for frame in scenario["frames"]] == [
            _timestamp_ns(index, info["fps"]) for index in range(1, info["frame_count"] + 1)
        ]
    assert declared == IDS
    declared_scenario = yaml.safe_load((SOURCE / "mot17-02" / "scenario.yaml").read_text())
    declared_ground_truth = (SOURCE / "mot17-02" / declared_scenario["ground_truth"]).resolve()
    assert declared_ground_truth == (ROOT / "data/motchallenge-v1/mot17-02/ground_truth.jsonl").resolve()

    # Exercise the loader's resolved-path contract without requiring the
    # content-addressed MOT media pack in a clean source checkout.
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"test-frame")
    ground_truth = tmp_path / "ground_truth.jsonl"
    ground_truth.write_text("")
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "cvbench.scenario/v1",
                "id": "path-contract",
                "family": "path-contract",
                "sequence_id": "path-contract",
                "ground_truth": ground_truth.name,
                "frames": [
                    {
                        "frame_index": 0,
                        "source_timestamp_ns": 0,
                        "width": 1,
                        "height": 1,
                        "path": frame.name,
                    }
                ],
            }
        )
    )
    loaded = load_scenario(scenario_path)
    assert loaded.ground_truth_path == ground_truth.resolve()


def test_visual_audit_and_public_annotation_bundles_are_hash_bound() -> None:
    ingest = json.loads((SOURCE / "ingest-manifest.json").read_text())
    audit = json.loads((SOURCE / "visual-audit.json").read_text())
    assert audit["manifest_sha256"] == ingest["manifest_sha256"]
    assert audit["audit_seed"] == ingest["audit_seed"]
    assert audit["review_status"] == "manual_review_completed"
    assert audit["manual_annotation_edits"] == []
    for scenario_id in IDS:
        sequence = audit["sequences"][scenario_id]
        assert sequence["selected_frame_count"] >= 60
        assert len(set(sequence["selected_frames_one_based"])) == sequence["selected_frame_count"]
        assert sequence["selected_track_count"] == len(sequence["tracks"]) == 12
        for kind in ("overview", "viewer_derivative", "annotation_bundle"):
            declaration = sequence[kind]
            body = (ROOT / declaration["path"]).read_bytes()
            assert len(body) == declaration["bytes"]
            assert _sha256(body) == declaration["sha256"]
        bundle = gzip.decompress((ROOT / sequence["annotation_bundle"]["path"]).read_bytes())
        assert _sha256(bundle) == sequence["normalized_gt_sha256"]


def test_normalization_is_one_based_pixel_edge_and_neutral_ignore_after_targets() -> None:
    spec = SEQUENCES[0]
    raw = (
        b"1,1,1,1,10,10,1,1,1\n"
        b"2,1,95,95,10,10,1,1,0\n"
        b"1,2,0,0,5,5,1,9,0.5\n"
    )
    rows, corrections, result = _normalize_gt(
        raw,
        spec=spec,
        fps=30,
        width=100,
        height=100,
        frame_count=2,
    )
    by_key = {(row["target_id"], row["source_timestamp_ns"]): row for row in rows}
    first = by_key[("person-0001", 0)]
    second = by_key[("person-0001", _timestamp_ns(2, 30))]
    ignored = by_key[("ignore-c09-0002", 0)]
    assert first["bbox_xyxy"] == [0, 0, 10, 10]
    assert first["eligible_for_detection"] is True
    assert ignored["ignore"] is True and ignored["ignore_region"] is True
    assert ignored["class_id"] == "person"
    assert second["bbox_xyxy"] == [94, 94, 100, 100]
    assert second["truncated"] is True and second["occlusion"] == "full"
    assert len(corrections) == 2
    assert result["scored_person_boxes"] == 2
    assert result["neutral_ignore_boxes"] == 1


def test_normalization_fails_closed_on_duplicate_and_id_class_drift() -> None:
    spec = SEQUENCES[0]
    with pytest.raises(RuntimeError, match="duplicate frame/id"):
        _normalize_gt(
            b"1,1,1,1,2,2,1,1,1\n1,1,1,1,2,2,1,1,1\n",
            spec=spec,
            fps=30,
            width=10,
            height=10,
            frame_count=1,
        )
    with pytest.raises(RuntimeError, match="ID/class drift"):
        _normalize_gt(
            b"1,1,1,1,2,2,1,1,1\n2,1,1,1,2,2,1,9,1\n",
            spec=spec,
            fps=30,
            width=10,
            height=10,
            frame_count=2,
        )
    with pytest.raises(RuntimeError, match="invalid box/visibility"):
        _normalize_gt(
            b"1,1,1,1,2,2,1,1,1.1\n",
            spec=spec,
            fps=30,
            width=10,
            height=10,
            frame_count=1,
        )
    with pytest.raises(RuntimeError, match="ordering drift"):
        _normalize_gt(
            b"2,1,1,1,2,2,1,1,1\n1,1,1,1,2,2,1,1,1\n",
            spec=spec,
            fps=30,
            width=10,
            height=10,
            frame_count=2,
        )


def test_archive_audit_rejects_parent_path_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", "not allowed")
    declaration = {"bytes": archive_path.stat().st_size, "sha256": _sha256(archive_path.read_bytes())}
    with pytest.raises(RuntimeError, match="unsafe inventory"):
        _audit_zip(archive_path, declaration)


def test_hydrated_verifier_binds_exact_inventory_and_bytes(tmp_path: Path) -> None:
    declarations = tmp_path / "scenarios" / "motchallenge-v1"
    output = tmp_path / "data" / "motchallenge-v1"
    frame = output / "mot17-02" / "frames" / "frame-000000.jpg"
    truth = output / "mot17-02" / "ground_truth.jsonl"
    frame.parent.mkdir(parents=True)
    truth.parent.mkdir(parents=True, exist_ok=True)
    declarations.mkdir(parents=True)
    frame.write_bytes(b"exact-frame")
    truth.write_bytes(b'{"exact":"truth"}\n')
    frame_relative = "mot17-02/frames/frame-000000.jpg"
    truth_relative = "mot17-02/ground_truth.jsonl"
    frame_line = f"{_sha256(frame.read_bytes())}  {frame_relative}"
    truth_line = f"{_sha256(truth.read_bytes())}  {truth_relative}"
    (declarations / "expected-frame-sha256.txt").write_text(frame_line + "\n")
    (declarations / "normalized-ground-truth-sha256.txt").write_text(truth_line + "\n")
    (output / "artifacts.sha256").write_text(f"{truth_line}\n{frame_line}\n")

    assert verify_hydrated(tmp_path) == output
    frame.write_bytes(b"drifted")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        verify_hydrated(tmp_path)
