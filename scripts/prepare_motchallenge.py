#!/usr/bin/env python3
"""Audit pinned MOTChallenge archives and prepare the ten approved dense sequences.

The input boundary is deliberately narrow: MOT16.zip supplies canonical pixels
for six MOT17 sequences, MOT17Labels.zip supplies their updated labels, and
MOT20.zip supplies four native MOT20 sequences.  No detections or other
representations are extracted.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ARCHIVES = {
    "MOT16.zip": {
        "bytes": 1_954_509_127,
        "sha256": "b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b",
        "url": "https://motchallenge.net/data/MOT16.zip",
        "retrieval_utc": "2026-07-24T01:45:04Z",
    },
    "MOT17Labels.zip": {
        "bytes": 10_107_022,
        "sha256": "0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9",
        "url": "https://motchallenge.net/data/MOT17Labels.zip",
        "retrieval_utc": "2026-07-24T01:45:04Z",
    },
    "MOT20.zip": {
        "bytes": 5_028_926_248,
        "sha256": "ebcf0e3d44e4f50b5357d24817e5db485d777633d1b8ca9e8380d1c8437dbdd7",
        "url": "https://motchallenge.net/data/MOT20.zip",
        "retrieval_utc": "2026-07-24T01:45:07Z",
    },
}
LICENSE = {
    "id": "CC-BY-NC-SA-3.0",
    "name": "Creative Commons Attribution-NonCommercial-ShareAlike 3.0 Unported",
    "url": "https://creativecommons.org/licenses/by-nc-sa/3.0/",
    "legalcode_url": "https://creativecommons.org/licenses/by-nc-sa/3.0/legalcode.txt",
    "legalcode_bytes": 22_306,
    "legalcode_sha256": "8812f83442fd0eca14eb0208988e190fdcbfebec58fa5459d3218edfdfdc5a32",
}
SCHEMA_VERSION = "cvbench.motchallenge-ingest/v1"
POLICY_VERSION = "cvbench.motchallenge-normalization/v1"
NANOSECONDS = 1_000_000_000
MOT17_NUMBERS = ("02", "04", "09", "10", "11", "13")
MOT20_NUMBERS = ("01", "02", "03", "05")
IGNORE_REGION_CLASSES = {9, 10, 11, 13}


@dataclass(frozen=True)
class SequenceSpec:
    scenario_id: str
    sequence_id: str
    archive_name: str
    image_root: str
    gt_members: tuple[str, ...]
    seqinfo_members: tuple[str, ...]
    expected_fps: int


SEQUENCES = tuple(
    SequenceSpec(
        scenario_id=f"mot17-{number}",
        sequence_id=f"MOT17-{number}",
        archive_name="MOT16.zip",
        image_root=f"train/MOT16-{number}",
        gt_members=tuple(f"train/MOT17-{number}-{variant}/gt/gt.txt" for variant in ("DPM", "FRCNN", "SDP")),
        seqinfo_members=tuple(
            f"train/MOT17-{number}-{variant}/seqinfo.ini" for variant in ("DPM", "FRCNN", "SDP")
        ),
        expected_fps=25 if number == "13" else 30,
    )
    for number in MOT17_NUMBERS
) + tuple(
    SequenceSpec(
        scenario_id=f"mot20-{number}",
        sequence_id=f"MOT20-{number}",
        archive_name="MOT20.zip",
        image_root=f"MOT20/train/MOT20-{number}",
        gt_members=(f"MOT20/train/MOT20-{number}/gt/gt.txt",),
        seqinfo_members=(f"MOT20/train/MOT20-{number}/seqinfo.ini",),
        expected_fps=25,
    )
    for number in MOT20_NUMBERS
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _integer(value: str, label: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RuntimeError(f"{label} is not numeric") from exc
    if parsed != parsed.to_integral_value():
        raise RuntimeError(f"{label} must be an integer")
    return int(parsed)


def _number(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RuntimeError(f"{label} is not numeric") from exc
    if not parsed.is_finite():
        raise RuntimeError(f"{label} must be finite")
    return parsed


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _timestamp_ns(frame_number: int, fps: int) -> int:
    numerator = (frame_number - 1) * NANOSECONDS
    return (2 * numerator + fps) // (2 * fps)


def _jpeg_dimensions(value: bytes) -> tuple[int, int]:
    if len(value) < 4 or value[:2] != b"\xff\xd8":
        raise RuntimeError("frame is not a JPEG")
    offset = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(value):
        if value[offset] != 0xFF:
            raise RuntimeError("malformed JPEG marker stream")
        marker = value[offset + 1]
        offset += 2
        while marker == 0xFF and offset < len(value):
            marker = value[offset]
            offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(value):
            break
        length = int.from_bytes(value[offset : offset + 2], "big")
        if length < 2 or offset + length > len(value):
            raise RuntimeError("malformed JPEG segment length")
        if marker in sof:
            if length < 7:
                raise RuntimeError("malformed JPEG dimensions")
            height = int.from_bytes(value[offset + 3 : offset + 5], "big")
            width = int.from_bytes(value[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    raise RuntimeError("JPEG dimensions are missing")


def _inventory_line(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0xFFFF
    return "|".join(
        (
            info.filename,
            f"{info.CRC:08x}",
            str(info.file_size),
            str(info.compress_size),
            str(info.compress_type),
            f"{mode:06o}",
        )
    )


def _audit_zip(path: Path, declaration: dict[str, Any]) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"archive is not a regular non-symlink file: {path}")
    actual_size = path.stat().st_size
    if actual_size != declaration["bytes"]:
        raise RuntimeError(f"{path.name} size drift: expected {declaration['bytes']}, got {actual_size}")
    digest = _sha256_file(path)
    if digest != declaration["sha256"]:
        raise RuntimeError(f"{path.name} hash drift: expected {declaration['sha256']}, got {digest}")
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    names = [info.filename for info in infos]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    folded: dict[str, list[str]] = defaultdict(list)
    unsafe: list[str] = []
    symlinks: list[str] = []
    special: list[str] = []
    for info in infos:
        name = info.filename
        folded[unicodedata.normalize("NFC", name).casefold()].append(name)
        pure = PurePosixPath(name)
        if (
            name.startswith(("/", "\\"))
            or "\\" in name
            or ".." in pure.parts
            or (pure.parts and ":" in pure.parts[0])
        ):
            unsafe.append(name)
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            symlinks.append(name)
        elif mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            special.append(name)
    collisions = [values for values in folded.values() if len(values) > 1 and len(set(values)) > 1]
    if duplicates or collisions or unsafe or symlinks or special:
        archive.close()
        raise RuntimeError(
            f"{path.name} unsafe inventory: duplicates={duplicates[:3]} collisions={collisions[:3]} "
            f"paths={unsafe[:3]} symlinks={symlinks[:3]} special={special[:3]}"
        )
    bad_crc = archive.testzip()
    if bad_crc is not None:
        archive.close()
        raise RuntimeError(f"{path.name} CRC failure at {bad_crc}")
    inventory = "\n".join(sorted(_inventory_line(info) for info in infos)).encode() + b"\n"
    suffixes = Counter(Path(name).suffix.lower() or "<none>" for name in names if not name.endswith("/"))
    roots = Counter(name.split("/", 1)[0] for name in names)
    return archive, {
        "member_count": len(infos),
        "file_count": sum(not info.is_dir() for info in infos),
        "directory_count": sum(info.is_dir() for info in infos),
        "compressed_member_bytes": sum(info.compress_size for info in infos),
        "uncompressed_member_bytes": sum(info.file_size for info in infos),
        "member_inventory_sha256": _sha256_bytes(inventory),
        "root_counts": dict(sorted(roots.items())),
        "suffix_counts": dict(sorted(suffixes.items())),
        "path_safety": {
            "absolute_or_parent_paths": 0,
            "casefold_collisions": 0,
            "duplicate_members": 0,
            "special_files": 0,
            "symlinks": 0,
        },
        "zip_crc": "verified",
    }


def _seqinfo(value: bytes, label: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(value.decode("utf-8-sig"))
        sequence = dict(parser["Sequence"])
    except (UnicodeDecodeError, configparser.Error, KeyError) as exc:
        raise RuntimeError(f"invalid seqinfo for {label}") from exc
    required = {"name", "imdir", "framerate", "seqlength", "imwidth", "imheight", "imext"}
    if set(sequence) != required:
        raise RuntimeError(f"{label} seqinfo fields drifted: {sorted(sequence)}")
    return sequence


def _normalized_seqinfo(value: dict[str, str]) -> dict[str, str]:
    return {key: item for key, item in value.items() if key != "name"}


def _occlusion(visibility: Decimal) -> str:
    if visibility == 1:
        return "none"
    if visibility == 0:
        return "full"
    return "partial"


def _normalize_gt(
    raw: bytes,
    *,
    spec: SequenceSpec,
    fps: int,
    width: int,
    height: int,
    frame_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    id_classes: dict[int, set[int]] = defaultdict(set)
    keys: set[tuple[int, int]] = set()
    upstream_classes: Counter[int] = Counter()
    score_ids: set[int] = set()
    score_boxes = 0
    ignore_ids: set[tuple[int, int]] = set()
    ignore_boxes = 0
    prior_source_key: tuple[int, int] | None = None
    try:
        source = io.StringIO(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{spec.sequence_id} GT is not UTF-8") from exc
    for line_number, row in enumerate(csv.reader(source), 1):
        if len(row) != 9:
            raise RuntimeError(f"{spec.sequence_id} GT line {line_number} has {len(row)} columns")
        frame = _integer(row[0], f"{spec.sequence_id}:{line_number} frame")
        upstream_id = _integer(row[1], f"{spec.sequence_id}:{line_number} id")
        x = _number(row[2], f"{spec.sequence_id}:{line_number} x")
        y = _number(row[3], f"{spec.sequence_id}:{line_number} y")
        box_width = _number(row[4], f"{spec.sequence_id}:{line_number} width")
        box_height = _number(row[5], f"{spec.sequence_id}:{line_number} height")
        mark = _integer(row[6], f"{spec.sequence_id}:{line_number} mark")
        class_id = _integer(row[7], f"{spec.sequence_id}:{line_number} class")
        visibility = _number(row[8], f"{spec.sequence_id}:{line_number} visibility")
        key = (frame, upstream_id)
        source_key = (upstream_id, frame)
        if prior_source_key is not None and source_key < prior_source_key:
            raise RuntimeError(f"{spec.sequence_id} GT ordering drift at line {line_number}")
        prior_source_key = source_key
        if key in keys:
            raise RuntimeError(f"{spec.sequence_id} duplicate frame/id at line {line_number}")
        keys.add(key)
        if not 1 <= frame <= frame_count or upstream_id < 1:
            raise RuntimeError(f"{spec.sequence_id} invalid frame/id at line {line_number}")
        if box_width <= 0 or box_height <= 0 or not 0 <= visibility <= 1:
            raise RuntimeError(f"{spec.sequence_id} invalid box/visibility at line {line_number}")
        if mark not in {0, 1}:
            raise RuntimeError(f"{spec.sequence_id} invalid mark at line {line_number}")
        id_classes[upstream_id].add(class_id)
        upstream_classes[class_id] += 1

        original = [x - 1, y - 1, x - 1 + box_width, y - 1 + box_height]
        clipped = [
            max(Decimal(0), original[0]),
            max(Decimal(0), original[1]),
            min(Decimal(width), original[2]),
            min(Decimal(height), original[3]),
        ]
        visible_box = clipped[0] < clipped[2] and clipped[1] < clipped[3]
        was_clipped = clipped != original
        scoring = mark == 1 and class_id == 1
        if scoring:
            score_ids.add(upstream_id)
            score_boxes += 1
            target_id = f"person-{upstream_id:04d}"
        else:
            ignore_ids.add((class_id, upstream_id))
            ignore_boxes += 1
            target_id = f"ignore-c{class_id:02d}-{upstream_id:04d}"
        record: dict[str, Any] = {
            "schema_version": "cvbench.ground-truth/v1",
            "target_id": target_id,
            "sequence_id": spec.sequence_id,
            "source_timestamp_ns": _timestamp_ns(frame, fps),
            "on_screen": visible_box,
            "eligible_for_detection": bool(scoring and visible_box),
            "visibility_fraction": _json_number(visibility),
            "occlusion": _occlusion(visibility),
            "class_id": "person",
            "ignore": not scoring,
            "ignore_region": bool(not scoring and class_id in IGNORE_REGION_CLASSES),
            "truncated": was_clipped,
        }
        if visible_box:
            record["bbox_xyxy"] = [_json_number(item) for item in clipped]
        if record["ignore_region"]:
            record["ignore_region_id"] = target_id
        records.append(record)
        if was_clipped or not visible_box:
            corrections.append(
                {
                    "sequence_id": spec.sequence_id,
                    "upstream_gt_sha256": _sha256_bytes(raw),
                    "upstream_line": line_number,
                    "upstream_frame": frame,
                    "upstream_id": upstream_id,
                    "upstream_mark": mark,
                    "upstream_class": class_id,
                    "original_one_based_xywh": [
                        _json_number(x),
                        _json_number(y),
                        _json_number(box_width),
                        _json_number(box_height),
                    ],
                    "normalized_unclipped_xyxy": [_json_number(item) for item in original],
                    "output_xyxy": [_json_number(item) for item in clipped] if visible_box else None,
                    "action": "clip_to_visible_frame_intersection" if visible_box else "retain_offscreen_without_box",
                }
            )
    drift = {str(identifier): sorted(classes) for identifier, classes in id_classes.items() if len(classes) != 1}
    if drift:
        raise RuntimeError(f"{spec.sequence_id} ID/class drift: {drift}")
    if records != sorted(records, key=lambda item: (item["source_timestamp_ns"], item["target_id"])):
        records.sort(key=lambda item: (item["source_timestamp_ns"], item["target_id"]))
    output_keys = {(row["source_timestamp_ns"], row["target_id"]) for row in records}
    if len(output_keys) != len(records):
        raise RuntimeError(f"{spec.sequence_id} normalized GT is not unique")
    return records, corrections, {
        "upstream_rows": len(records),
        "upstream_classes": {str(key): value for key, value in sorted(upstream_classes.items())},
        "scored_person_tracks": len(score_ids),
        "scored_person_boxes": score_boxes,
        "neutral_ignore_tracks": len(ignore_ids),
        "neutral_ignore_boxes": ignore_boxes,
        "boundary_corrections": len(corrections),
        "fully_offscreen_rows": sum(item["action"] == "retain_offscreen_without_box" for item in corrections),
    }


def _assert_output_record(row: dict[str, Any], *, width: int, height: int, timestamps: set[int]) -> None:
    required = {
        "schema_version",
        "target_id",
        "sequence_id",
        "source_timestamp_ns",
        "on_screen",
        "eligible_for_detection",
        "visibility_fraction",
        "occlusion",
        "class_id",
        "ignore",
        "ignore_region",
        "truncated",
    }
    if not required <= set(row) or row["schema_version"] != "cvbench.ground-truth/v1":
        raise RuntimeError("normalized GT contract drift")
    if row["class_id"] != "person" or row["source_timestamp_ns"] not in timestamps:
        raise RuntimeError("normalized GT ontology/timestamp drift")
    if not isinstance(row["visibility_fraction"], (int, float)) or isinstance(row["visibility_fraction"], bool):
        raise RuntimeError("normalized GT visibility type drift")
    if not 0 <= row["visibility_fraction"] <= 1:
        raise RuntimeError("normalized GT visibility range drift")
    if row["occlusion"] not in {"none", "partial", "full"}:
        raise RuntimeError("normalized GT occlusion drift")
    if row["ignore"] and row["eligible_for_detection"]:
        raise RuntimeError("ignore row became detection-eligible")
    box = row.get("bbox_xyxy")
    if row["on_screen"] != bool(box):
        raise RuntimeError("normalized GT on-screen/box mismatch")
    if box and not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
        raise RuntimeError("normalized GT contains an out-of-bounds box")


def _scenario_manifest(spec: SequenceSpec, info: dict[str, Any]) -> dict[str, Any]:
    frames = [
        {
            "frame_index": index,
            "source_timestamp_ns": _timestamp_ns(index + 1, info["fps"]),
            "width": info["width"],
            "height": info["height"],
            "path": f"../../../data/motchallenge-v1/{spec.scenario_id}/frames/frame-{index:06d}.jpg",
        }
        for index in range(info["frame_count"])
    ]
    return {
        "schema_version": "cvbench.scenario/v1",
        "id": spec.scenario_id,
        "family": f"motchallenge-source-{spec.scenario_id}",
        "sequence_id": spec.sequence_id,
        "license": LICENSE["id"],
        "source": "MOTChallenge pinned official archives; see docs/motchallenge-sources.md",
        "annotation_scope": "exhaustive_full_frame_pedestrians_with_neutral_ignore",
        "ontology": ["person"],
        "ground_truth": f"../../../data/motchallenge-v1/{spec.scenario_id}/ground_truth.jsonl",
        "frames": frames,
    }


def _write_repository_files(
    repo_root: Path,
    *,
    provenance: dict[str, Any],
    frame_hashes: list[str],
    gt_hashes: list[str],
    corrections: list[dict[str, Any]],
    scenario_manifests: dict[str, dict[str, Any]],
) -> None:
    root = repo_root / "scenarios" / "motchallenge-v1"
    root.mkdir(parents=True, exist_ok=True)
    (root / "ingest-manifest.json").write_bytes(_canonical_json(provenance))
    (root / "expected-frame-sha256.txt").write_text("\n".join(frame_hashes) + "\n")
    (root / "normalized-ground-truth-sha256.txt").write_text("\n".join(gt_hashes) + "\n")
    (root / "corrections.jsonl").write_bytes(b"".join(_canonical_json(row) for row in corrections))
    for scenario_id, manifest in scenario_manifests.items():
        directory = root / scenario_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "scenario.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def _archive_source_directory(repo_root: Path, requested: Path | None) -> Path:
    if requested is not None:
        return requested.resolve()
    environment = os.environ.get("CVBENCH_MOTCHALLENGE_INGEST")
    if environment:
        return Path(environment).resolve()
    return (repo_root / ".local-ingest" / "motchallenge").resolve()


def _download_official(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, declaration in ARCHIVES.items():
        target = destination / name
        if target.exists():
            continue
        temporary = target.with_suffix(".partial")
        request = urllib.request.Request(declaration["url"], headers={"User-Agent": "cvbench-ingest/1"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=8 * 1024 * 1024)
        temporary.replace(target)


def prepare(
    repo_root: Path,
    *,
    ingest: Path | None = None,
    output: Path | None = None,
    write_repository_files: bool = False,
    allow_official_download: bool = False,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    ingest_root = _archive_source_directory(repo_root, ingest)
    output = (output or repo_root / "data" / "motchallenge-v1").resolve()
    expected_output = (repo_root / "data" / "motchallenge-v1").resolve()
    if output != expected_output:
        raise RuntimeError("output must be the dedicated data/motchallenge-v1 directory")
    if allow_official_download and not all((ingest_root / name).is_file() for name in ARCHIVES):
        _download_official(ingest_root)
    missing = [name for name in ARCHIVES if not (ingest_root / name).is_file()]
    if missing:
        raise RuntimeError(f"missing pinned official archive(s) in {ingest_root}: {', '.join(missing)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    archives: dict[str, zipfile.ZipFile] = {}
    archive_audits: dict[str, Any] = {}
    for name, declaration in ARCHIVES.items():
        archive, audit = _audit_zip(ingest_root / name, declaration)
        archives[name] = archive
        archive_audits[name] = {**declaration, **audit}

    staging = Path(tempfile.mkdtemp(prefix="motchallenge-v1-", dir=output.parent))
    frame_hashes: list[str] = []
    gt_hashes: list[str] = []
    all_corrections: list[dict[str, Any]] = []
    sequence_results: dict[str, Any] = {}
    scenario_manifests: dict[str, dict[str, Any]] = {}
    selected_member_hashes: dict[str, str] = {}
    try:
        for spec in SEQUENCES:
            image_archive = archives[spec.archive_name]
            label_archive = archives["MOT17Labels.zip"] if spec.sequence_id.startswith("MOT17") else image_archive
            pixel_seqinfo_raw = image_archive.read(f"{spec.image_root}/seqinfo.ini")
            selected_member_hashes[f"{spec.archive_name}:{spec.image_root}/seqinfo.ini"] = _sha256_bytes(
                pixel_seqinfo_raw
            )
            pixel_info = _seqinfo(pixel_seqinfo_raw, spec.sequence_id)
            fps = int(pixel_info["framerate"])
            frame_count = int(pixel_info["seqlength"])
            width = int(pixel_info["imwidth"])
            height = int(pixel_info["imheight"])
            if (
                fps != spec.expected_fps
                or pixel_info["imdir"] != "img1"
                or pixel_info["imext"].lower() != ".jpg"
                or frame_count <= 0
                or width <= 0
                or height <= 0
            ):
                raise RuntimeError(f"{spec.sequence_id} cadence/geometry drift")

            gt_values = [label_archive.read(member) for member in spec.gt_members]
            label_archive_name = "MOT17Labels.zip" if spec.sequence_id.startswith("MOT17") else spec.archive_name
            for member, value in zip(spec.gt_members, gt_values, strict=True):
                selected_member_hashes[f"{label_archive_name}:{member}"] = _sha256_bytes(value)
            if len(set(gt_values)) != 1:
                raise RuntimeError(f"{spec.sequence_id} detector GT copies are not byte-identical")
            label_infos = []
            for member in spec.seqinfo_members:
                value = label_archive.read(member)
                selected_member_hashes[f"{label_archive_name}:{member}"] = _sha256_bytes(value)
                label_infos.append(_seqinfo(value, spec.sequence_id))
            if any(_normalized_seqinfo(value) != _normalized_seqinfo(pixel_info) for value in label_infos):
                raise RuntimeError(f"{spec.sequence_id} detector seqinfo copies drift from canonical pixels")

            expected_members = [f"{spec.image_root}/img1/{index:06d}.jpg" for index in range(1, frame_count + 1)]
            names = set(image_archive.namelist())
            missing_frames = [name for name in expected_members if name not in names]
            actual_frames = {
                name
                for name in names
                if name.startswith(f"{spec.image_root}/img1/") and name.lower().endswith(".jpg")
            }
            if missing_frames or actual_frames != set(expected_members):
                raise RuntimeError(f"{spec.sequence_id} missing/extra frame members")

            scenario_root = staging / spec.scenario_id
            frame_root = scenario_root / "frames"
            frame_root.mkdir(parents=True)
            for index, member in enumerate(expected_members):
                value = image_archive.read(member)
                if _jpeg_dimensions(value) != (width, height):
                    raise RuntimeError(f"{spec.sequence_id} frame {index + 1} dimension drift")
                relative = f"{spec.scenario_id}/frames/frame-{index:06d}.jpg"
                digest = _sha256_bytes(value)
                frame_hashes.append(f"{digest}  {relative}")
                (frame_root / f"frame-{index:06d}.jpg").write_bytes(value)

            rows, corrections, stats = _normalize_gt(
                gt_values[0],
                spec=spec,
                fps=fps,
                width=width,
                height=height,
                frame_count=frame_count,
            )
            timestamps = {_timestamp_ns(index, fps) for index in range(1, frame_count + 1)}
            for row in rows:
                _assert_output_record(row, width=width, height=height, timestamps=timestamps)
            gt_body = b"".join(_canonical_json(row) for row in rows)
            (scenario_root / "ground_truth.jsonl").write_bytes(gt_body)
            gt_digest = _sha256_bytes(gt_body)
            gt_hashes.append(f"{gt_digest}  {spec.scenario_id}/ground_truth.jsonl")
            all_corrections.extend(corrections)
            result = {
                "sequence_id": spec.sequence_id,
                "scenario_id": spec.scenario_id,
                "fps": fps,
                "frame_count": frame_count,
                "duration_seconds": frame_count / fps,
                "width": width,
                "height": height,
                "canonical_pixel_archive": spec.archive_name,
                "canonical_pixel_root": spec.image_root,
                "updated_gt_sha256": _sha256_bytes(gt_values[0]),
                "normalized_gt_sha256": gt_digest,
                "detector_gt_copy_count": len(gt_values),
                "detector_gt_copies_byte_identical": len(set(gt_values)) == 1,
                "label_archive_contains_images": False if spec.sequence_id.startswith("MOT17") else None,
                **stats,
            }
            sequence_results[spec.scenario_id] = result
            scenario_manifests[spec.scenario_id] = _scenario_manifest(spec, result)

        total = {
            "sequences": len(sequence_results),
            "frames": sum(value["frame_count"] for value in sequence_results.values()),
            "duration_seconds": sum(value["duration_seconds"] for value in sequence_results.values()),
            "scored_person_tracks": sum(value["scored_person_tracks"] for value in sequence_results.values()),
            "scored_person_boxes": sum(value["scored_person_boxes"] for value in sequence_results.values()),
            "neutral_ignore_tracks": sum(value["neutral_ignore_tracks"] for value in sequence_results.values()),
            "neutral_ignore_boxes": sum(value["neutral_ignore_boxes"] for value in sequence_results.values()),
            "published_headline_trajectories_including_mot20_ignore": 2_745,
            "published_headline_boxes_including_mot20_ignore": 1_442_300,
            "boundary_corrections": len(all_corrections),
        }
        if total["frames"] != 13_410 or abs(total["duration_seconds"] - 511.54) > 1e-9:
            raise RuntimeError(f"selected tranche cardinality drift: {total}")
        if total["scored_person_tracks"] != 2_628 or total["scored_person_boxes"] != 1_239_994:
            raise RuntimeError(f"selected class-1 ontology drift: {total}")
        if total["scored_person_tracks"] + sum(
            sequence_results[key]["neutral_ignore_tracks"] for key in sequence_results if key.startswith("mot20-")
        ) != 2_745:
            raise RuntimeError("published trajectory envelope no longer reproduces")
        if total["scored_person_boxes"] + sum(
            sequence_results[key]["neutral_ignore_boxes"] for key in sequence_results if key.startswith("mot20-")
        ) != 1_442_300:
            raise RuntimeError("published box envelope no longer reproduces")

        frame_manifest_body = ("\n".join(frame_hashes) + "\n").encode()
        gt_manifest_body = ("\n".join(gt_hashes) + "\n").encode()
        corrections_body = b"".join(_canonical_json(row) for row in all_corrections)
        scenario_manifest_hashes = {
            scenario_id: _sha256_bytes(yaml.safe_dump(manifest, sort_keys=False).encode())
            for scenario_id, manifest in sorted(scenario_manifests.items())
        }
        provenance_without_hash = {
            "schema_version": SCHEMA_VERSION,
            "normalization_policy": POLICY_VERSION,
            "official_archives_only": True,
            "public_detections_used": False,
            "mots_or_mot15_representations_used": False,
            "archive_audits": archive_audits,
            "selected_member_sha256": dict(sorted(selected_member_hashes.items())),
            "license": LICENSE,
            "license_boundary": {
                "repository_code": "repository LICENSE",
                "motchallenge_assets_and_derivatives": LICENSE["id"],
                "noncommercial_only": True,
                "share_alike_required": True,
            },
            "cadence_disclosure": (
                "Timestamps are derived from one-based ordered JPEG ordinals and publisher-declared fixed FPS as "
                "(frame-1)/FPS, rounded to the nearest integer nanosecond. Original container PTS is unavailable "
                "and is not claimed."
            ),
            "mot17_pixel_policy": (
                "MOT17Labels.zip contains no image members. DPM/FRCNN/SDP GT bytes are equal; exactly one "
                "canonical MOT16 pixel sequence is used and public detections are excluded."
            ),
            "selected_sequence_ids": [spec.sequence_id for spec in SEQUENCES],
            "sequence_results": sequence_results,
            "totals": total,
            "expected_frame_manifest_sha256": _sha256_bytes(frame_manifest_body),
            "normalized_ground_truth_manifest_sha256": _sha256_bytes(gt_manifest_body),
            "corrections_sha256": _sha256_bytes(corrections_body),
            "scenario_manifest_sha256": scenario_manifest_hashes,
            "correction_policy": (
                "MOT one-based xywh is converted to pixel-edge xyxy. Boundary-crossing boxes are clipped only to "
                "their visible frame intersection; fully offscreen rows are retained with on_screen=false and no "
                "box. Every changed row is listed in corrections.jsonl."
            ),
        }
        manifest_hash = _sha256_bytes(_canonical_json(provenance_without_hash))
        provenance = {**provenance_without_hash, "manifest_sha256": manifest_hash, "audit_seed": manifest_hash}
        (staging / "artifacts.sha256").write_text(
            "\n".join(
                f"{_sha256_file(path)}  {path.relative_to(staging).as_posix()}"
                for path in sorted(staging.rglob("*"))
                if path.is_file() and path.name != "artifacts.sha256"
            )
            + "\n"
        )
        if output.exists():
            shutil.rmtree(output)
        staging.replace(output)
        if write_repository_files:
            _write_repository_files(
                repo_root,
                provenance=provenance,
                frame_hashes=frame_hashes,
                gt_hashes=gt_hashes,
                corrections=all_corrections,
                scenario_manifests=scenario_manifests,
            )
        return provenance
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        for archive in archives.values():
            archive.close()


def verify(repo_root: Path, *, ingest: Path | None = None) -> dict[str, Any]:
    expected = json.loads((repo_root / "scenarios" / "motchallenge-v1" / "ingest-manifest.json").read_text())
    actual = prepare(repo_root, ingest=ingest, write_repository_files=False)
    if actual != expected:
        raise RuntimeError("deterministic MOTChallenge regeneration differs from the committed manifest")
    return actual


def _read_hash_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise RuntimeError(f"missing prepared MOTChallenge hash manifest: {path}") from exc
    result: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        digest, separator, relative = line.partition("  ")
        relative_path = PurePosixPath(relative)
        if (
            separator != "  "
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in result
        ):
            raise RuntimeError(f"invalid MOTChallenge hash manifest entry at {path}:{line_number}")
        result[relative] = digest
    if not result:
        raise RuntimeError(f"empty MOTChallenge hash manifest: {path}")
    return result


def verify_hydrated(repo_root: Path) -> Path:
    """Fail closed unless the prepared corpus exactly matches committed hashes."""
    repo_root = repo_root.resolve()
    output = repo_root / "data" / "motchallenge-v1"
    if output.is_symlink() or not output.is_dir():
        raise RuntimeError(f"prepared MOTChallenge corpus is missing: {output}")

    frame_hashes = _read_hash_manifest(repo_root / "scenarios/motchallenge-v1/expected-frame-sha256.txt")
    truth_hashes = _read_hash_manifest(
        repo_root / "scenarios/motchallenge-v1/normalized-ground-truth-sha256.txt"
    )
    if set(frame_hashes) & set(truth_hashes):
        raise RuntimeError("committed MOTChallenge hash manifests contain duplicate paths")
    expected = {**frame_hashes, **truth_hashes}
    declared = _read_hash_manifest(output / "artifacts.sha256")
    if declared != expected:
        raise RuntimeError("prepared MOTChallenge artifact inventory differs from committed hashes")

    entries = list(output.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise RuntimeError("prepared MOTChallenge corpus contains a symlink")
    actual = {
        path.relative_to(output).as_posix()
        for path in entries
        if path.is_file() and path.name != "artifacts.sha256"
    }
    if actual != set(expected):
        raise RuntimeError("prepared MOTChallenge corpus contains missing or extra artifacts")
    for relative, digest in expected.items():
        if _sha256_file(output / relative) != digest:
            raise RuntimeError(f"prepared MOTChallenge artifact hash mismatch: {relative}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ingest", type=Path)
    parser.add_argument("--write-repository-files", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--allow-official-download", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    result = (
        verify(repo_root, ingest=args.ingest)
        if args.verify
        else prepare(
            repo_root,
            ingest=args.ingest,
            write_repository_files=args.write_repository_files,
            allow_official_download=args.allow_official_download,
        )
    )
    print(json.dumps({"manifest_sha256": result["manifest_sha256"], "totals": result["totals"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
