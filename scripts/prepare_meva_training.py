#!/usr/bin/env python3
"""Prepare user-supplied MEVA KF1 training annotations for local model training.

This importer is deliberately local-only. It never downloads MEVA data, emits
only positive activity-bounded labels, and marks every result as ineligible for
CVBench evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import cv2
import yaml

from cvbench.protocol import validate_bbox, validate_ground_truth

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INGEST_ROOT = ROOT / ".local-ingest" / "meva"
MEVA_ANNOTATION_ORIGINS = {
    "https://gitlab.kitware.com/meva/meva-data-repo",
    "git@gitlab.kitware.com:meva/meva-data-repo",
    "ssh://git@gitlab.kitware.com/meva/meva-data-repo",
}
MEVA_LICENSE_URL = "https://mevadata.org/resources/MEVA-data-license.txt"
MEVA_LICENSE_SHA256 = "bdeedfb765049c87f92a2450369ad70882fca3371190b2a6b7e560e103c922e8"
MEVA_ATTRIBUTION = (
    "Multiview Extended Video with Activities (MEVA) dataset by Kitware Inc. "
    "and the Intelligence Advanced Research Projects Activity (IARPA)"
)
EXPECTED_FPS = 30
JPEG_QUALITY = 95
SCENARIO_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to(path: Path, root: Path, description: str) -> Path:
    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{description} must be inside {root}") from exc


def _local_file(path: Path, ingest_root: Path, description: str) -> Path:
    ingest_root = ingest_root.resolve()
    candidate = path if path.is_absolute() else ingest_root / path
    relative = _relative_to(candidate.absolute(), ingest_root, description)
    cursor = ingest_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeError(f"{description} must not traverse a symlink: {cursor}")
    resolved = candidate.resolve(strict=True)
    _relative_to(resolved, ingest_root, description)
    if not resolved.is_file():
        raise RuntimeError(f"{description} is not a regular file: {resolved}")
    return resolved


def _output_path(ingest_root: Path, scenario_id: str) -> Path:
    output = ingest_root.resolve() / "prepared" / scenario_id
    relative = _relative_to(output, ingest_root.resolve(), "output")
    if not relative.parts or relative.parts[0] != "prepared":
        raise RuntimeError("output must be below the local prepared directory")
    cursor = ingest_root.resolve()
    for part in relative.parts[:-1]:
        cursor /= part
        if cursor.exists() and cursor.is_symlink():
            raise RuntimeError(f"output must not traverse a symlink: {cursor}")
    return output


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _verify_annotation_checkout(geom_path: Path, types_path: Path) -> tuple[Path, str, str, str]:
    repo = Path(_git(geom_path.parent, "rev-parse", "--show-toplevel")).resolve()
    if _relative_to(geom_path, repo, "geometry annotation").parts[:4] != (
        "annotation",
        "DIVA-phase-2",
        "MEVA",
        "kitware-meva-training",
    ):
        raise RuntimeError("geometry annotation is not from the official kitware-meva-training tree")
    _relative_to(types_path, repo, "type annotation")
    if Path(_git(types_path.parent, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise RuntimeError("geometry and type annotations must come from the same checkout")

    origin = _git(repo, "config", "--get", "remote.origin.url").removesuffix(".git").rstrip("/")
    if origin not in MEVA_ANNOTATION_ORIGINS:
        raise RuntimeError(f"annotation checkout origin is not the official MEVA repository: {origin}")
    commit = _git(repo, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("annotation checkout HEAD is not a full Git commit")

    relatives = []
    for path in (geom_path, types_path):
        relative = path.relative_to(repo).as_posix()
        _git(repo, "ls-files", "--error-unmatch", relative)
        if _git(repo, "status", "--porcelain", "--untracked-files=all", "--", relative):
            raise RuntimeError(f"annotation file is not clean at {commit}: {relative}")
        relatives.append(relative)
    return repo, commit, relatives[0], relatives[1]


def _records(path: Path, kind: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            parsed = yaml.safe_load(line)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"invalid {kind} YAML at {path}:{line_number}") from exc
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise RuntimeError(f"invalid {kind} record at {path}:{line_number}")
        record = parsed[0].get(kind)
        if not isinstance(record, dict):
            raise RuntimeError(f"missing {kind} mapping at {path}:{line_number}")
        records.append(record)
    if not records:
        raise RuntimeError(f"{path} contains no {kind} records")
    return records


def _parse_annotations(
    geom_path: Path,
    types_path: Path,
) -> tuple[dict[int, str], dict[int, dict[int, list[int]]]]:
    classes: dict[int, str] = {}
    for record in _records(types_path, "types"):
        try:
            track_id = int(record["id1"])
            class_set = record["cset3"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid type record in {types_path}") from exc
        if not isinstance(class_set, dict) or len(class_set) != 1:
            raise RuntimeError(f"track {track_id} does not have exactly one source class")
        class_id = next(iter(class_set))
        if not isinstance(class_id, str) or not class_id:
            raise RuntimeError(f"track {track_id} has an invalid source class")
        previous = classes.setdefault(track_id, class_id)
        if previous != class_id:
            raise RuntimeError(f"track {track_id} has conflicting source classes")

    tracks: dict[int, dict[int, list[int]]] = {}
    for record in _records(geom_path, "geom"):
        try:
            track_id = int(record["id1"])
            frame_index = int(record["ts0"])
            raw_box = record["g0"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid geometry record in {geom_path}") from exc
        if track_id not in classes:
            raise RuntimeError(f"geometry track {track_id} has no source class")
        if frame_index < 0 or not isinstance(raw_box, str):
            raise RuntimeError(f"invalid geometry for track {track_id} at frame {frame_index}")
        try:
            box = [int(value) for value in raw_box.split()]
        except ValueError as exc:
            raise RuntimeError(f"non-integer geometry for track {track_id} at frame {frame_index}") from exc
        validate_bbox(box)
        frames = tracks.setdefault(track_id, {})
        if frame_index in frames:
            raise RuntimeError(f"duplicate geometry for track {track_id} at frame {frame_index}")
        frames[frame_index] = box
    if not tracks:
        raise RuntimeError(f"{geom_path} contains no usable geometry")
    return classes, tracks


def _timestamp_ns(frame_index: int) -> int:
    return round(frame_index * 1_000_000_000 / EXPECTED_FPS)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))


def _write_artifact_manifest(output: Path) -> None:
    entries = [
        f"{_sha256(path)}  {path.relative_to(output).as_posix()}"
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "artifacts.sha256"
    ]
    (output / "artifacts.sha256").write_text("\n".join(entries) + "\n")


def verify_prepared(output: Path) -> str:
    output = output.resolve()
    artifacts_path = output / "artifacts.sha256"
    declared: dict[str, str] = {}
    for line in artifacts_path.read_text().splitlines():
        digest, separator, relative = line.partition("  ")
        if separator != "  " or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("invalid artifacts.sha256 entry")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or relative in declared:
            raise RuntimeError("unsafe or duplicate artifacts.sha256 path")
        declared[relative] = digest
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != "artifacts.sha256"
    }
    if set(declared) != actual:
        raise RuntimeError("artifacts.sha256 does not cover the exact prepared output")
    for relative, expected in declared.items():
        path = output / relative
        if path.is_symlink() or _sha256(path) != expected:
            raise RuntimeError(f"prepared artifact checksum mismatch: {relative}")

    manifest = yaml.safe_load((output / "scenario.yaml").read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "cvbench.scenario/v1":
        raise RuntimeError("invalid prepared scenario manifest")
    if manifest.get("data_role") != "model_training_only" or manifest.get("evaluation_eligible") is not False:
        raise RuntimeError("prepared scenario is not fail-closed as training-only")
    policy = manifest.get("annotation_policy")
    if policy != {
        "scope": "activity_bounded_positive_only",
        "exhaustive": False,
        "missing_annotation_semantics": "unknown_not_background",
    }:
        raise RuntimeError("prepared scenario annotation policy is invalid")

    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise RuntimeError("prepared scenario has no positive training frames")
    frame_by_timestamp: dict[int, dict[str, Any]] = {}
    last_timestamp = -1
    for frame in frames:
        if not isinstance(frame, dict) or frame.get("annotation_state") != "positive_only":
            raise RuntimeError("invalid prepared training frame")
        timestamp = frame.get("source_timestamp_ns")
        source_index = frame.get("source_frame_index")
        if (
            not isinstance(timestamp, int)
            or not isinstance(source_index, int)
            or timestamp != _timestamp_ns(source_index)
            or timestamp <= last_timestamp
        ):
            raise RuntimeError("prepared frame timestamps do not preserve source frame time")
        relative = Path(str(frame.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or not (output / relative).is_file():
            raise RuntimeError("prepared frame path is unsafe or missing")
        frame_by_timestamp[timestamp] = frame
        last_timestamp = timestamp

    rows = []
    for line in (output / "ground_truth.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = validate_ground_truth(json.loads(line))
        frame = frame_by_timestamp.get(row["source_timestamp_ns"])
        if frame is None:
            raise RuntimeError("training row timestamp has no prepared frame")
        if (
            row.get("training_only") is not True
            or row.get("annotation_scope") != "activity_bounded_positive"
            or row.get("source_frame_index") != frame["source_frame_index"]
            or row["visibility_fraction"] is not None
            or row["occlusion"] != "unknown"
        ):
            raise RuntimeError("training row loses the positive-only or unknown-visibility boundary")
        validate_bbox(row["bbox_xyxy"], width=frame["width"], height=frame["height"])
        rows.append(row)
    if not rows or {row["source_timestamp_ns"] for row in rows} != set(frame_by_timestamp):
        raise RuntimeError("every prepared frame must contain at least one positive annotation")
    if {row["class_id"] for row in rows} != set(manifest.get("ontology", [])):
        raise RuntimeError("prepared ontology does not match the source classes")

    provenance = json.loads((output / "provenance.json").read_text())
    if (
        provenance.get("schema_version") != "cvbench.meva-training-provenance/v1"
        or provenance.get("data_role") != "model_training_only"
        or provenance.get("license", {}).get("sha256") != MEVA_LICENSE_SHA256
    ):
        raise RuntimeError("invalid MEVA training provenance")
    return _sha256(artifacts_path)


def prepare_sequence(
    *,
    ingest_root: Path,
    scenario_id: str,
    video_path: Path,
    geom_path: Path,
    types_path: Path,
    license_path: Path,
    output: Path | None = None,
) -> Path:
    ingest_root = ingest_root.resolve()
    if ingest_root.name != "meva" or ingest_root.parent.name != ".local-ingest":
        raise RuntimeError("ingest root must end in .local-ingest/meva")
    if not SCENARIO_ID.fullmatch(scenario_id):
        raise RuntimeError("scenario id must be a lowercase ASCII slug")

    video_path = _local_file(video_path, ingest_root, "video")
    geom_path = _local_file(geom_path, ingest_root, "geometry annotation")
    types_path = _local_file(types_path, ingest_root, "type annotation")
    license_path = _local_file(license_path, ingest_root, "MEVA license")
    for path, expected_parent in ((video_path, "videos"), (geom_path, "annotations"), (types_path, "annotations")):
        if _relative_to(path, ingest_root, "input").parts[0] != expected_parent:
            raise RuntimeError(f"{path.name} must be below the local {expected_parent} directory")
    if _sha256(license_path) != MEVA_LICENSE_SHA256:
        raise RuntimeError("local MEVA license does not match the pinned official CC BY 4.0 text")

    if not geom_path.name.endswith(".geom.yml") or not types_path.name.endswith(".types.yml"):
        raise RuntimeError("MEVA annotations must use .geom.yml and .types.yml filenames")
    stem = geom_path.name.removesuffix(".geom.yml")
    if types_path.name != f"{stem}.types.yml" or video_path.name != f"{stem}.r13.avi":
        raise RuntimeError("video, geometry, and type annotation stems do not match")

    _annotation_repo, annotation_commit, geom_relative, types_relative = _verify_annotation_checkout(
        geom_path, types_path
    )
    classes, tracks = _parse_annotations(geom_path, types_path)
    annotated_frames = sorted({frame for frames in tracks.values() for frame in frames})

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"could not open MEVA video: {video_path}")
    width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    frame_count = round(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0 or frame_count <= 0 or abs(fps - EXPECTED_FPS) > 1e-6:
        capture.release()
        raise RuntimeError(
            f"MEVA video metadata mismatch: expected 30 FPS with positive dimensions/count, "
            f"got {width}x{height}, {fps} FPS, {frame_count} frames"
        )
    if annotated_frames[-1] >= frame_count:
        capture.release()
        raise RuntimeError("annotation references a frame outside the source video")
    for track_id, frames in tracks.items():
        for frame_index, box in frames.items():
            try:
                validate_bbox(box, width=width, height=height)
            except Exception as exc:
                capture.release()
                raise RuntimeError(f"out-of-bounds geometry for track {track_id} at frame {frame_index}") from exc

    output = output.resolve() if output else _output_path(ingest_root, scenario_id)
    output_relative = _relative_to(output, ingest_root, "output")
    if not output_relative.parts or output_relative.parts[0] != "prepared":
        capture.release()
        raise RuntimeError("output must be below the local prepared directory")
    if output.exists():
        capture.release()
        raise RuntimeError(f"output already exists: {output}")
    staging = output.with_name(f"{output.name}.tmp")
    if staging.exists():
        capture.release()
        raise RuntimeError(f"staging output already exists: {staging}")

    try:
        frames_dir = staging / "frames"
        frames_dir.mkdir(parents=True)
        wanted = set(annotated_frames)
        decoded = 0
        while decoded < frame_count:
            ok, image = capture.read()
            if not ok:
                raise RuntimeError(f"source video ended before declared frame {decoded}")
            if image.shape[1] != width or image.shape[0] != height:
                raise RuntimeError(f"source video dimensions changed at frame {decoded}")
            if decoded in wanted:
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if not ok:
                    raise RuntimeError(f"could not encode source frame {decoded}")
                (frames_dir / f"frame-{decoded:06d}.jpg").write_bytes(encoded.tobytes())
            decoded += 1
        if capture.read()[0]:
            raise RuntimeError("source video contains more frames than its declared frame count")
        capture.release()

        rows: list[dict[str, Any]] = []
        for track_id in sorted(tracks):
            for frame_index, box in sorted(tracks[track_id].items()):
                rows.append(
                    {
                        "annotation_scope": "activity_bounded_positive",
                        "bbox_xyxy": box,
                        "class_id": classes[track_id],
                        "eligible_for_detection": True,
                        "occlusion": "unknown",
                        "on_screen": True,
                        "schema_version": "cvbench.ground-truth/v1",
                        "sequence_id": scenario_id,
                        "source_frame_index": frame_index,
                        "source_timestamp_ns": _timestamp_ns(frame_index),
                        "source_track_id": track_id,
                        "target_id": str(track_id),
                        "training_only": True,
                        "visibility_fraction": None,
                    }
                )
        rows.sort(key=lambda row: (row["source_timestamp_ns"], row["source_track_id"]))
        _write_jsonl(staging / "ground_truth.jsonl", rows)

        manifest = {
            "schema_version": "cvbench.scenario/v1",
            "id": scenario_id,
            "family": "meva_kf1_training_activity_objects",
            "sequence_id": scenario_id,
            "data_role": "model_training_only",
            "evaluation_eligible": False,
            "license": "CC-BY-4.0",
            "license_url": MEVA_LICENSE_URL,
            "attribution": MEVA_ATTRIBUTION,
            "source": f"MEVA KF1 training-level annotations at {annotation_commit}",
            "annotation_policy": {
                "scope": "activity_bounded_positive_only",
                "exhaustive": False,
                "missing_annotation_semantics": "unknown_not_background",
            },
            "ontology": sorted({classes[track_id] for track_id in tracks}),
            "ground_truth": "ground_truth.jsonl",
            "provenance": "provenance.json",
            "frames": [
                {
                    "frame_index": frame_index,
                    "source_frame_index": frame_index,
                    "source_timestamp_ns": _timestamp_ns(frame_index),
                    "width": width,
                    "height": height,
                    "path": f"frames/frame-{frame_index:06d}.jpg",
                    "annotation_state": "positive_only",
                }
                for frame_index in annotated_frames
            ],
        }
        (staging / "scenario.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

        provenance = {
            "schema_version": "cvbench.meva-training-provenance/v1",
            "data_role": "model_training_only",
            "source_dataset": {
                "name": "MEVA Known Facility 1",
                "annotation_level": "training",
                "annotation_scope": "activity_bounded_positive_only",
                "missing_annotation_semantics": "unknown_not_background",
            },
            "license": {
                "id": "CC-BY-4.0",
                "url": MEVA_LICENSE_URL,
                "sha256": _sha256(license_path),
                "attribution": MEVA_ATTRIBUTION,
            },
            "source_video": {
                "filename": video_path.name,
                "bytes": video_path.stat().st_size,
                "sha256": _sha256(video_path),
                "width": width,
                "height": height,
                "fps": {"numerator": EXPECTED_FPS, "denominator": 1},
                "frame_count": frame_count,
            },
            "source_annotations": {
                "repository": "https://gitlab.kitware.com/meva/meva-data-repo",
                "commit": annotation_commit,
                "files": {
                    "geom": {"path": geom_relative, "sha256": _sha256(geom_path)},
                    "types": {"path": types_relative, "sha256": _sha256(types_path)},
                },
            },
            "importer": {
                "path": "scripts/prepare_meva_training.py",
                "sha256": _sha256(Path(__file__)),
                "opencv": cv2.__version__,
                "jpeg_quality": JPEG_QUALITY,
            },
            "output": {
                "scenario_sha256": _sha256(staging / "scenario.yaml"),
                "ground_truth_sha256": _sha256(staging / "ground_truth.jsonl"),
                "positive_frame_count": len(annotated_frames),
                "positive_annotation_count": len(rows),
            },
        }
        (staging / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
        )
        _write_artifact_manifest(staging)
        verify_prepared(staging)
        output.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(output)
    except Exception:
        capture.release()
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingest-root", type=Path, default=DEFAULT_INGEST_ROOT)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--geom", type=Path)
    parser.add_argument("--types", type=Path)
    parser.add_argument("--license", type=Path, default=Path("MEVA-data-license.txt"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    output = _output_path(args.ingest_root, args.scenario_id)
    if args.verify_only:
        print(verify_prepared(output))
        return 0
    if args.video is None or args.geom is None or args.types is None:
        parser.error("--video, --geom, and --types are required unless --verify-only is used")
    prepared = prepare_sequence(
        ingest_root=args.ingest_root,
        scenario_id=args.scenario_id,
        video_path=args.video,
        geom_path=args.geom,
        types_path=args.types,
        license_path=args.license,
        output=output,
    )
    print(prepared)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
