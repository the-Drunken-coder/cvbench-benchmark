from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from cvbench.config import Thresholds
from cvbench.metrics import calculate_metrics
from cvbench.model import CollectedRecord
from scripts.hydrate_real_video_corpus import _extract_frames, _validated_archive

ROOT = Path(__file__).parents[1]
CLIP_IDS = ("rvmot-a1c9", "rvmot-b7e2", "rvmot-c4f6")
FPS = 30
FRAME_COUNT = 150


def _manifest(clip_id: str) -> dict:
    return yaml.safe_load((ROOT / "scenarios" / "real-video-v2" / clip_id / "scenario.yaml").read_text())


def _rows(clip_id: str) -> list[dict]:
    path = ROOT / "scenarios" / "real-video-v2" / clip_id / "ground_truth.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _output(row: dict, *, track_id: str | None = None, box: list[float] | None = None) -> CollectedRecord:
    record = {
        "schema_version": "cvbench.track/v1",
        "event": "track_update",
        "sequence_id": row["sequence_id"],
        "source_timestamp_ns": row["source_timestamp_ns"],
        "track_id": track_id or row["target_id"],
        "state": "confirmed",
        "support": "observed",
        "class_id": row["class_id"],
        "confidence": 0.9,
        "geometry": {
            "type": "bbox_xyxy",
            "space": "source_pixels",
            "value": box or row["bbox_xyxy"],
        },
    }
    return CollectedRecord(row["source_timestamp_ns"] + 1_000_000, record)


def test_real_scenarios_are_full_frame_native_cadence_and_have_no_ignores() -> None:
    for clip_id in CLIP_IDS:
        manifest = _manifest(clip_id)
        rows = _rows(clip_id)
        assert manifest["id"] == clip_id
        assert manifest["annotation_scope"] == "exhaustive_full_frame_moving_objects"
        assert manifest["ontology"] == ["person", "vehicle", "dog"]
        assert "scoreable_roi" not in manifest
        assert len(manifest["frames"]) == FRAME_COUNT
        assert manifest["frames"][0]["source_timestamp_ns"] == 0
        assert manifest["frames"][-1]["source_timestamp_ns"] == round((FRAME_COUNT - 1) * 1_000_000_000 / FPS)
        intervals = [
            right["source_timestamp_ns"] - left["source_timestamp_ns"]
            for left, right in zip(manifest["frames"], manifest["frames"][1:], strict=False)
        ]
        assert set(intervals) == {33_333_333, 33_333_334}
        assert all(
            frame["source_timestamp_ns"] == round(frame["frame_index"] * 1_000_000_000 / 30)
            for frame in manifest["frames"]
        )
        assert all(not row.get("ignore", False) for row in rows)
        assert all(row["on_screen"] and row["eligible_for_detection"] for row in rows)
        assert {row["class_id"] for row in rows} <= {"person", "vehicle", "dog"}
        assert {row["class_id"] for row in rows} >= {"person", "vehicle"}


def test_public_suite_includes_every_synthetic_and_replacement_real_scenario() -> None:
    public = yaml.safe_load((ROOT / "benchmarks" / "public-whole-system-v3.yaml").read_text())
    declared = {
        yaml.safe_load((ROOT / "benchmarks" / relative).resolve().read_text())["id"]
        for relative in public["scenarios"]
    }
    expected_synthetic = {
        yaml.safe_load(path.read_text())["id"]
        for path in (ROOT / "scenarios" / "synthetic-v1").glob("*/scenario.yaml")
    }
    assert public["id"] == "public-whole-system-tracking"
    assert public["version"] == "3.0.0"
    assert public["resources"] == {"cpu_limit": 4, "memory_limit_mb": 8192, "network_access": False}
    assert public["max_run_seconds"] == 240
    assert declared == expected_synthetic | set(CLIP_IDS)
    assert len(declared) == 16


def test_compatibility_truth_is_frame_unique() -> None:
    for clip_id in CLIP_IDS:
        rows = _rows(clip_id)
        keys = [(row["source_timestamp_ns"], row["target_id"]) for row in rows]
        assert len(keys) == len(set(keys))
        assert all(isinstance(row["truncated"], bool) for row in rows)
        assert all(row["occlusion"] == "unknown" for row in rows)
        assert all(row["visibility_fraction"] is None for row in rows)
        archive = ROOT / "scenarios" / "real-video-v2" / "archives" / f"{clip_id}.visual-audit.tar"
        assert archive.is_file()


def test_visual_audit_closes_every_supported_mover_span() -> None:
    audit = json.loads((ROOT / "scenarios" / "real-video-v2" / "visual-audit.json").read_text())
    assert audit["schema_version"] == "cvbench.real-video-visual-audit/v1"
    assert set(audit["clips"]) == set(CLIP_IDS)
    assert all(item["omitted_supported_movers"] == [] for item in audit["clips"].values())
    frame_manifest = (ROOT / "scenarios" / "real-video-v2" / "expected-frame-sha256.txt").read_bytes()
    assert audit["frame_manifest_sha256"] == hashlib.sha256(frame_manifest).hexdigest()
    spans = {}
    for clip_id in CLIP_IDS:
        rows = _rows(clip_id)
        for target in {row["target_id"] for row in rows}:
            timestamps = [row["source_timestamp_ns"] for row in rows if row["target_id"] == target]
            spans[(clip_id, target)] = (
                round(min(timestamps) * 30 / 1e9),
                round(max(timestamps) * 30 / 1e9),
            )
    assert spans[("rvmot-b7e2", "v-001")] == (0, 149)
    assert spans[("rvmot-c4f6", "p-004")] == (32, 149)
    assert spans[("rvmot-c4f6", "v-003")] == (0, 149)


def test_expected_frame_manifest_covers_exact_corpus() -> None:
    lines = (ROOT / "scenarios" / "real-video-v2" / "expected-frame-sha256.txt").read_text().splitlines()
    assert len(lines) == len(CLIP_IDS) * FRAME_COUNT
    assert len({line.split("  ", 1)[1] for line in lines}) == len(lines)
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)


def test_hydrator_rejects_nonregular_tar_entries(tmp_path: Path) -> None:
    archive = tmp_path / "frames.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as handle:
        for index in range(FRAME_COUNT):
            member = tarfile.TarInfo(f"frames/frame-{index:04d}.jpg")
            if index == 0:
                member.type = tarfile.SYMTYPE
                member.linkname = "../secret"
                handle.addfile(member)
            else:
                member.size = 1
                handle.addfile(member, io.BytesIO(b"x"))
    expected = {f"canary/frames/frame-{index:04d}.jpg": "0" * 64 for index in range(FRAME_COUNT)}
    with pytest.raises(RuntimeError, match="non-regular"):
        _extract_frames(archive, tmp_path / "output", "canary", expected)


def test_hydrator_rejects_ancestor_directory_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    archive_root = real / "scenarios" / "real-video-v2" / "archives"
    archive_root.mkdir(parents=True)
    archive = archive_root / "rvmot-a1c9.frames.tar"
    archive.write_bytes(b"tar")
    linked_repo = tmp_path / "repo"
    linked_repo.mkdir()
    (linked_repo / "scenarios").symlink_to(real / "scenarios", target_is_directory=True)
    declaration = {
        "bytes": archive.stat().st_size,
        "path": "scenarios/real-video-v2/archives/rvmot-a1c9.frames.tar",
        "sha256": "0" * 64,
    }
    with pytest.raises(RuntimeError, match="symlink"):
        _validated_archive(linked_repo, declaration, "rvmot-a1c9")


def test_perfect_multi_object_tracking_scores_perfect_hota_and_idf1() -> None:
    rows = _rows("rvmot-b7e2")
    collected = [_output(row) for row in rows]
    metrics, _ = calculate_metrics(rows, collected, Thresholds())
    mot = metrics["multi_object_tracking"]
    assert mot["hota"] == pytest.approx(1.0)
    assert mot["idf1"] == pytest.approx(1.0)
    assert mot["ground_truth_tracks"] == 3
    assert mot["ground_truth_detections"] == len(rows)
    assert all(item["hota"] == pytest.approx(1.0) for item in mot["hota_by_iou_threshold"])


def test_identity_switch_misses_and_false_tracks_reduce_mot_scores() -> None:
    rows = _rows("rvmot-b7e2")
    collected = []
    for index, row in enumerate(rows):
        if row["target_id"] == "p-001" and index % 4 == 0:
            continue
        track_id = row["target_id"]
        if row["target_id"] == "p-002" and row["source_timestamp_ns"] >= 2_500_000_000:
            track_id = "p-002-switched"
        collected.append(_output(row, track_id=track_id))
    exemplar = rows[0]
    for timestamp in sorted({row["source_timestamp_ns"] for row in rows})[:30]:
        false_row = {**exemplar, "source_timestamp_ns": timestamp, "target_id": "false"}
        collected.append(_output(false_row, track_id="false-track", box=[600, 20, 680, 90]))
    metrics, _ = calculate_metrics(rows, collected, Thresholds())
    mot = metrics["multi_object_tracking"]
    assert 0 < mot["hota"] < 1
    assert 0 < mot["idf1"] < 1
    assert mot["identity_false_negatives"] > 0
    assert mot["identity_false_positives"] > 0
    assert metrics["identity"]["id_switches"] > 0


def test_real_ground_truth_is_never_part_of_frame_protocol_metadata() -> None:
    runner = (ROOT / "src" / "cvbench" / "runner.py").read_text()
    metadata_block = runner[runner.index('metadata = {', runner.index("def _deliver_scenarios")) :]
    metadata_block = metadata_block[
        : metadata_block.index("_send_before_deadline(connection, metadata")
    ]
    assert "bbox_xyxy" not in metadata_block
    assert "ground_truth" not in metadata_block
    assert "target_id" not in metadata_block
    assert "scoreable_roi" not in metadata_block


def test_real_video_docs_state_evaluation_not_required_training_data() -> None:
    docs = (ROOT / "docs" / "real-video-sources.md").read_text()
    assert "CC BY 4.0" in docs
    assert "evaluation data" in docs
    assert "not model training data" in docs
    assert "never sent to submitted systems" in docs
