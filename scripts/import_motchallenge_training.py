#!/usr/bin/env python3
"""Import pinned official MOT17/MOT20 training data into CVBench manifests.

The importer never downloads data. MOT17's three detector variants are reduced
to one video by pairing the updated, byte-identical MOT17 labels with the
canonical MOT16 pixels from which MOT17 was published.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import io
import json
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

SCHEMA_VERSION = "cvbench.motchallenge-training-corpus/v1"
MAPPING_POLICY = "cvbench.motchallenge-training-mapping/v1"
NANOSECONDS = 1_000_000_000


@dataclass(frozen=True)
class ArchiveRequirement:
    bytes: int
    sha256: str
    url: str
    accepted_official_bytes_utc: str


@dataclass(frozen=True)
class SequenceSpec:
    sequence_id: str
    pixel_archive: str
    pixel_root: str
    annotation_archive: str
    gt_members: tuple[str, ...]
    seqinfo_members: tuple[str, ...]
    detector_variants: tuple[str, ...]

    @property
    def slug(self) -> str:
        return self.sequence_id.lower()


# These bytes were retrieved directly from the named official URLs on
# 2026-07-24. MOTChallenge does not publish archive checksums or version tags,
# so accepted official bytes are content-addressed here and drift fails closed.
ARCHIVES = {
    "MOT16.zip": ArchiveRequirement(
        bytes=1_954_509_127,
        sha256="b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b",
        url="https://motchallenge.net/data/MOT16.zip",
        accepted_official_bytes_utc="2026-07-24T01:45:04Z",
    ),
    "MOT17Labels.zip": ArchiveRequirement(
        bytes=10_107_022,
        sha256="0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9",
        url="https://motchallenge.net/data/MOT17Labels.zip",
        accepted_official_bytes_utc="2026-07-24T01:45:04Z",
    ),
    "MOT20.zip": ArchiveRequirement(
        bytes=5_028_926_248,
        sha256="ebcf0e3d44e4f50b5357d24817e5db485d777633d1b8ca9e8380d1c8437dbdd7",
        url="https://motchallenge.net/data/MOT20.zip",
        accepted_official_bytes_utc="2026-07-24T01:45:07Z",
    ),
}

MOT17_NUMBERS = ("02", "04", "05", "09", "10", "11", "13")
MOT20_NUMBERS = ("01", "02", "03", "05")
MOT17_VARIANTS = ("DPM", "FRCNN", "SDP")

SEQUENCES = tuple(
    SequenceSpec(
        sequence_id=f"MOT17-{number}",
        pixel_archive="MOT16.zip",
        pixel_root=f"train/MOT16-{number}",
        annotation_archive="MOT17Labels.zip",
        gt_members=tuple(f"train/MOT17-{number}-{variant}/gt/gt.txt" for variant in MOT17_VARIANTS),
        seqinfo_members=tuple(f"train/MOT17-{number}-{variant}/seqinfo.ini" for variant in MOT17_VARIANTS),
        detector_variants=MOT17_VARIANTS,
    )
    for number in MOT17_NUMBERS
) + tuple(
    SequenceSpec(
        sequence_id=f"MOT20-{number}",
        pixel_archive="MOT20.zip",
        pixel_root=f"MOT20/train/MOT20-{number}",
        annotation_archive="MOT20.zip",
        gt_members=(f"MOT20/train/MOT20-{number}/gt/gt.txt",),
        seqinfo_members=(f"MOT20/train/MOT20-{number}/seqinfo.ini",),
        detector_variants=(),
    )
    for number in MOT20_NUMBERS
)

MOT_CLASSES = {
    1: "person",
    2: "person_on_vehicle",
    3: "car",
    4: "bicycle",
    5: "motorbike",
    6: "non_motorized_vehicle",
    7: "static_person",
    8: "distractor",
    9: "occluder",
    10: "occluder_on_ground",
    11: "occluder_full",
    12: "reflection",
    13: "crowd",
}
DISTRACTOR_CLASSES = {2, 7, 8, 12}
IGNORE_REGION_CLASSES = {9, 10, 11, 13}

LICENSE = {
    "id": "CC-BY-NC-SA-3.0",
    "name": "Creative Commons Attribution-NonCommercial-ShareAlike 3.0 Unported",
    "url": "https://creativecommons.org/licenses/by-nc-sa/3.0/",
    "legalcode_url": "https://creativecommons.org/licenses/by-nc-sa/3.0/legalcode.txt",
    "legalcode_bytes": 22_306,
    "legalcode_sha256": "8812f83442fd0eca14eb0208988e190fdcbfebec58fa5459d3218edfdfdc5a32",
    "attribution": "MOTChallenge (https://motchallenge.net/), MOT17 and MOT20 datasets.",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name:
        raise RuntimeError(f"unsafe ZIP member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise RuntimeError(f"unsafe ZIP member path: {name!r}")
    return unicodedata.normalize("NFC", name).casefold()


def _audit_archive(path: Path, requirement: ArchiveRequirement) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"archive is not a regular non-symlink file: {path}")
    actual_bytes = path.stat().st_size
    if actual_bytes != requirement.bytes:
        raise RuntimeError(f"{path.name} size drift: expected {requirement.bytes}, got {actual_bytes}")
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != requirement.sha256:
        raise RuntimeError(f"{path.name} SHA-256 drift: expected {requirement.sha256}, got {actual_sha256}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"{path.name} is not a valid ZIP archive") from exc

    names: set[str] = set()
    casefolded: set[str] = set()
    inventory: list[str] = []
    files = 0
    directories = 0
    try:
        for info in archive.infolist():
            folded = _safe_member_name(info.filename)
            if info.filename in names:
                raise RuntimeError(f"{path.name} contains duplicate member {info.filename}")
            if folded in casefolded:
                raise RuntimeError(f"{path.name} contains case-colliding member {info.filename}")
            names.add(info.filename)
            casefolded.add(folded)
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                raise RuntimeError(f"{path.name} contains symlink member {info.filename}")
            if mode and kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise RuntimeError(f"{path.name} contains special member {info.filename}")
            if info.is_dir():
                directories += 1
            else:
                files += 1
            inventory.append(
                "|".join(
                    (
                        info.filename,
                        f"{info.CRC:08x}",
                        str(info.file_size),
                        str(info.compress_size),
                        str(info.compress_type),
                        f"{mode:06o}",
                    )
                )
            )
        bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError(f"{path.name} failed CRC validation at {bad_member}")
    except Exception:
        archive.close()
        raise

    return archive, {
        "bytes": actual_bytes,
        "sha256": actual_sha256,
        "url": requirement.url,
        "accepted_official_bytes_utc": requirement.accepted_official_bytes_utc,
        "member_count": len(inventory),
        "file_count": files,
        "directory_count": directories,
        "member_inventory_sha256": _sha256_bytes(("\n".join(inventory) + "\n").encode()),
        "zip_crc": "verified",
        "path_safety": "verified",
    }


def _read_member(archive: zipfile.ZipFile, archive_name: str, member: str) -> bytes:
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise RuntimeError(f"{archive_name} is missing required member {member}") from exc
    if info.is_dir():
        raise RuntimeError(f"{archive_name} required member is a directory: {member}")
    return archive.read(info)


def _parse_seqinfo(value: bytes, label: str) -> dict[str, str]:
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


def _integer(value: str, label: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RuntimeError(f"{label} is not numeric") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise RuntimeError(f"{label} must be a finite integer")
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
    return int(value) if value == value.to_integral_value() else float(value)


def _timestamp_ns(frame_number: int, fps: int) -> int:
    numerator = (frame_number - 1) * NANOSECONDS
    return (2 * numerator + fps) // (2 * fps)


def _jpeg_dimensions(value: bytes) -> tuple[int, int]:
    if len(value) < 4 or value[:2] != b"\xff\xd8":
        raise RuntimeError("frame is not a JPEG")
    offset = 2
    start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
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
            raise RuntimeError("malformed JPEG segment")
        if marker in start_of_frame:
            if length < 7:
                raise RuntimeError("malformed JPEG dimensions")
            height = int.from_bytes(value[offset + 3 : offset + 5], "big")
            width = int.from_bytes(value[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    raise RuntimeError("JPEG dimensions are missing")


def _occlusion(visibility: Decimal) -> str:
    if visibility == 1:
        return "none"
    if visibility == 0:
        return "full"
    return "partial"


def _normalize_ground_truth(
    raw: bytes,
    *,
    sequence_id: str,
    fps: int,
    width: int,
    height: int,
    frame_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        source = io.StringIO(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{sequence_id} GT is not UTF-8") from exc

    records: list[dict[str, Any]] = []
    keys: set[tuple[int, int]] = set()
    id_classes: dict[int, set[int]] = defaultdict(set)
    class_rows: Counter[int] = Counter()
    class_tracks: dict[int, set[int]] = defaultdict(set)
    scored_rows = 0
    ignored_rows = 0
    ignore_region_rows = 0
    truncated_rows = 0
    offscreen_rows = 0

    for line_number, row in enumerate(csv.reader(source), 1):
        if len(row) != 9:
            raise RuntimeError(f"{sequence_id} GT line {line_number} has {len(row)} columns")
        frame = _integer(row[0], f"{sequence_id}:{line_number} frame")
        source_id = _integer(row[1], f"{sequence_id}:{line_number} id")
        x = _number(row[2], f"{sequence_id}:{line_number} x")
        y = _number(row[3], f"{sequence_id}:{line_number} y")
        box_width = _number(row[4], f"{sequence_id}:{line_number} width")
        box_height = _number(row[5], f"{sequence_id}:{line_number} height")
        mark = _integer(row[6], f"{sequence_id}:{line_number} mark")
        source_class = _integer(row[7], f"{sequence_id}:{line_number} class")
        visibility = _number(row[8], f"{sequence_id}:{line_number} visibility")

        key = (frame, source_id)
        if key in keys:
            raise RuntimeError(f"{sequence_id} duplicate frame/id at line {line_number}")
        keys.add(key)
        if not 1 <= frame <= frame_count or source_id < 1:
            raise RuntimeError(f"{sequence_id} invalid frame/id at line {line_number}")
        if box_width <= 0 or box_height <= 0 or not 0 <= visibility <= 1:
            raise RuntimeError(f"{sequence_id} invalid box/visibility at line {line_number}")
        if mark not in {0, 1}:
            raise RuntimeError(f"{sequence_id} invalid mark at line {line_number}")
        if source_class not in MOT_CLASSES:
            raise RuntimeError(f"{sequence_id} unknown MOT class {source_class} at line {line_number}")

        id_classes[source_id].add(source_class)
        class_rows[source_class] += 1
        class_tracks[source_class].add(source_id)
        source_xywh = [x, y, box_width, box_height]
        source_xyxy = [x - 1, y - 1, x - 1 + box_width, y - 1 + box_height]
        clipped = [
            max(Decimal(0), source_xyxy[0]),
            max(Decimal(0), source_xyxy[1]),
            min(Decimal(width), source_xyxy[2]),
            min(Decimal(height), source_xyxy[3]),
        ]
        on_screen = clipped[0] < clipped[2] and clipped[1] < clipped[3]
        truncated = source_xyxy != clipped
        official_target = mark == 1 and source_class == 1
        ignore_region = not official_target and source_class in IGNORE_REGION_CLASSES
        target_id = f"{sequence_id}:mot-id:{source_id:06d}"
        record: dict[str, Any] = {
            "schema_version": "cvbench.ground-truth/v1",
            "target_id": target_id,
            "sequence_id": sequence_id,
            "source_timestamp_ns": _timestamp_ns(frame, fps),
            "on_screen": on_screen,
            "eligible_for_detection": bool(official_target and on_screen),
            "visibility_fraction": _json_number(visibility),
            "visibility_known": True,
            "occlusion": _occlusion(visibility),
            "class_id": MOT_CLASSES[source_class],
            "ignore": not official_target,
            "ignore_region": ignore_region,
            "evaluation_state": "score" if official_target else "ignore",
            "distractor": source_class in DISTRACTOR_CLASSES,
            "truncated": truncated,
            "source_mot": {
                "frame": frame,
                "id": source_id,
                "mark": mark,
                "class_id": source_class,
                "bbox_xywh": [_json_number(item) for item in source_xywh],
                "bbox_xyxy_unclipped": [_json_number(item) for item in source_xyxy],
                "visibility": _json_number(visibility),
            },
        }
        if on_screen:
            record["bbox_xyxy"] = [_json_number(item) for item in clipped]
        if ignore_region:
            record["ignore_region_id"] = target_id
        records.append(record)

        scored_rows += official_target
        ignored_rows += not official_target
        ignore_region_rows += ignore_region
        truncated_rows += truncated
        offscreen_rows += not on_screen

    drift = {str(identifier): sorted(classes) for identifier, classes in id_classes.items() if len(classes) != 1}
    if drift:
        raise RuntimeError(f"{sequence_id} ID/class drift: {drift}")
    records.sort(key=lambda item: (item["source_timestamp_ns"], item["target_id"]))
    output_keys = {(item["source_timestamp_ns"], item["target_id"]) for item in records}
    if len(output_keys) != len(records):
        raise RuntimeError(f"{sequence_id} normalized GT is not unique")
    return records, {
        "rows": len(records),
        "tracks": len(id_classes),
        "scored_person_rows": scored_rows,
        "ignored_rows": ignored_rows,
        "ignore_region_rows": ignore_region_rows,
        "truncated_rows": truncated_rows,
        "fully_offscreen_rows": offscreen_rows,
        "class_rows": {str(key): value for key, value in sorted(class_rows.items())},
        "class_tracks": {str(key): len(value) for key, value in sorted(class_tracks.items())},
    }


def _sequence_manifest(
    spec: SequenceSpec,
    *,
    fps: int,
    width: int,
    height: int,
    frame_count: int,
    ontology: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "cvbench.scenario/v1",
        "id": f"motchallenge-training-{spec.slug}",
        "family": "external-training-motchallenge",
        "sequence_id": spec.sequence_id,
        "training_only": True,
        "license": LICENSE["id"],
        "attribution": LICENSE["attribution"],
        "source": (
            f"https://motchallenge.net/data/{'MOT17' if spec.sequence_id.startswith('MOT17') else 'MOT20'}/"
        ),
        "mapping_policy": MAPPING_POLICY,
        "native_fps": fps,
        "timestamp_policy": "nearest integer nanosecond to (one_based_frame - 1) / native_fps",
        "annotation_scope": "official_motchallenge_training_ground_truth",
        "ontology": ontology,
        "ground_truth": "ground_truth.jsonl",
        "frames": [
            {
                "frame_index": index - 1,
                "source_timestamp_ns": _timestamp_ns(index, fps),
                "width": width,
                "height": height,
                "path": f"frames/{index:06d}.jpg",
            }
            for index in range(1, frame_count + 1)
        ],
        "faults": [],
    }


def _artifact_manifest(root: Path) -> bytes:
    lines = [
        f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in {"artifacts.sha256", "corpus-manifest.json"}
    ]
    return ("\n".join(lines) + "\n").encode()


def verify_output(output: Path) -> dict[str, Any]:
    output = output.resolve()
    manifest_path = output / "corpus-manifest.json"
    artifact_path = output / "artifacts.sha256"
    try:
        manifest = json.loads(manifest_path.read_text())
        artifact_body = artifact_path.read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read training corpus metadata in {output}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("training corpus manifest schema drift")
    payload = dict(manifest)
    recorded_payload_hash = payload.pop("manifest_payload_sha256", None)
    if recorded_payload_hash != _sha256_bytes(_canonical_json(payload)):
        raise RuntimeError("training corpus manifest payload hash mismatch")
    if manifest.get("output_artifact_manifest_sha256") != _sha256_bytes(artifact_body):
        raise RuntimeError("training corpus artifact manifest hash mismatch")

    output_paths = list(output.rglob("*"))
    symlinks = [path.relative_to(output).as_posix() for path in output_paths if path.is_symlink()]
    if symlinks:
        raise RuntimeError(f"training corpus contains symlink: {symlinks[0]}")
    listed: dict[str, str] = {}
    for line in artifact_body.decode().splitlines():
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise RuntimeError("malformed training corpus artifact manifest") from exc
        if len(digest) != 64 or _safe_member_name(relative) != unicodedata.normalize("NFC", relative).casefold():
            raise RuntimeError(f"malformed training corpus artifact entry: {line}")
        if relative in listed:
            raise RuntimeError(f"duplicate training corpus artifact entry: {relative}")
        listed[relative] = digest
    actual = {
        path.relative_to(output).as_posix()
        for path in output_paths
        if path.is_file() and path.name not in {"artifacts.sha256", "corpus-manifest.json"}
    }
    if set(listed) != actual:
        raise RuntimeError("training corpus artifact inventory mismatch")
    for relative, digest in listed.items():
        if _sha256_file(output / relative) != digest:
            raise RuntimeError(f"training corpus artifact hash mismatch: {relative}")
    return manifest


def import_corpus(ingest: Path, output: Path) -> dict[str, Any]:
    ingest = ingest.resolve()
    output = output.resolve()
    missing = [name for name in ARCHIVES if not (ingest / name).is_file()]
    if missing:
        raise RuntimeError(f"missing pinned official archive(s) in {ingest}: {', '.join(missing)}")
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"output already exists; refusing to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    archives: dict[str, zipfile.ZipFile] = {}
    archive_audits: dict[str, Any] = {}
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    selected_member_hashes: dict[str, str] = {}
    sequence_results: dict[str, Any] = {}
    try:
        for name, requirement in ARCHIVES.items():
            archive, audit = _audit_archive(ingest / name, requirement)
            archives[name] = archive
            archive_audits[name] = audit

        for spec in SEQUENCES:
            pixel_archive = archives[spec.pixel_archive]
            annotation_archive = archives[spec.annotation_archive]
            pixel_seqinfo_member = f"{spec.pixel_root}/seqinfo.ini"
            pixel_seqinfo_raw = _read_member(pixel_archive, spec.pixel_archive, pixel_seqinfo_member)
            selected_member_hashes[f"{spec.pixel_archive}:{pixel_seqinfo_member}"] = _sha256_bytes(pixel_seqinfo_raw)
            pixel_info = _parse_seqinfo(pixel_seqinfo_raw, spec.sequence_id)
            try:
                fps = int(pixel_info["framerate"])
                frame_count = int(pixel_info["seqlength"])
                width = int(pixel_info["imwidth"])
                height = int(pixel_info["imheight"])
            except ValueError as exc:
                raise RuntimeError(f"{spec.sequence_id} has non-integer cadence/geometry") from exc
            if (
                fps <= 0
                or frame_count <= 0
                or width <= 0
                or height <= 0
                or pixel_info["imdir"] != "img1"
                or pixel_info["imext"].lower() != ".jpg"
            ):
                raise RuntimeError(f"{spec.sequence_id} cadence/geometry drift")

            gt_values = [
                _read_member(annotation_archive, spec.annotation_archive, member) for member in spec.gt_members
            ]
            for member, value in zip(spec.gt_members, gt_values, strict=True):
                selected_member_hashes[f"{spec.annotation_archive}:{member}"] = _sha256_bytes(value)
            if len(set(gt_values)) != 1:
                raise RuntimeError(f"{spec.sequence_id} detector GT copies are not byte-identical")
            label_infos = []
            for member in spec.seqinfo_members:
                value = _read_member(annotation_archive, spec.annotation_archive, member)
                selected_member_hashes[f"{spec.annotation_archive}:{member}"] = _sha256_bytes(value)
                label_infos.append(_parse_seqinfo(value, spec.sequence_id))
            if any(_normalized_seqinfo(value) != _normalized_seqinfo(pixel_info) for value in label_infos):
                raise RuntimeError(f"{spec.sequence_id} detector seqinfo copies drift from canonical pixels")

            expected_members = [
                f"{spec.pixel_root}/img1/{index:06d}.jpg" for index in range(1, frame_count + 1)
            ]
            actual_members = {
                name
                for name in pixel_archive.namelist()
                if name.startswith(f"{spec.pixel_root}/img1/") and name.lower().endswith(".jpg")
            }
            if set(expected_members) != actual_members:
                raise RuntimeError(f"{spec.sequence_id} has missing or extra frame members")

            sequence_root = staging / "sequences" / spec.slug
            frame_root = sequence_root / "frames"
            frame_root.mkdir(parents=True)
            frame_hash_lines = []
            for index, member in enumerate(expected_members, 1):
                value = _read_member(pixel_archive, spec.pixel_archive, member)
                if _jpeg_dimensions(value) != (width, height):
                    raise RuntimeError(f"{spec.sequence_id} frame {index} dimension drift")
                target = frame_root / f"{index:06d}.jpg"
                target.write_bytes(value)
                frame_hash_lines.append(f"{_sha256_bytes(value)}  frames/{target.name}")
            frame_manifest_body = ("\n".join(frame_hash_lines) + "\n").encode()
            (sequence_root / "frames.sha256").write_bytes(frame_manifest_body)

            records, statistics = _normalize_ground_truth(
                gt_values[0],
                sequence_id=spec.sequence_id,
                fps=fps,
                width=width,
                height=height,
                frame_count=frame_count,
            )
            ground_truth_body = b"".join(_canonical_json(record) for record in records)
            (sequence_root / "ground_truth.jsonl").write_bytes(ground_truth_body)
            ontology = sorted({record["class_id"] for record in records})
            scenario = _sequence_manifest(
                spec,
                fps=fps,
                width=width,
                height=height,
                frame_count=frame_count,
                ontology=ontology,
            )
            scenario_body = yaml.safe_dump(scenario, sort_keys=False).encode()
            (sequence_root / "scenario.yaml").write_bytes(scenario_body)
            sequence_results[spec.slug] = {
                "sequence_id": spec.sequence_id,
                "native_fps": fps,
                "frame_count": frame_count,
                "duration_seconds": frame_count / fps,
                "width": width,
                "height": height,
                "canonical_pixel_source": f"{spec.pixel_archive}:{spec.pixel_root}",
                "annotation_source": spec.annotation_archive,
                "detector_variants_collapsed": list(spec.detector_variants),
                "detector_gt_copies": len(gt_values),
                "detector_gt_copies_byte_identical": len(set(gt_values)) == 1,
                "source_gt_sha256": _sha256_bytes(gt_values[0]),
                "frame_manifest_sha256": _sha256_bytes(frame_manifest_body),
                "ground_truth_sha256": _sha256_bytes(ground_truth_body),
                "scenario_manifest_sha256": _sha256_bytes(scenario_body),
                "ontology": ontology,
                **statistics,
            }

        artifact_body = _artifact_manifest(staging)
        (staging / "artifacts.sha256").write_bytes(artifact_body)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mapping_policy": MAPPING_POLICY,
            "training_only": True,
            "official_archives_only": True,
            "downloads_performed": False,
            "archives": archive_audits,
            "selected_member_sha256": dict(sorted(selected_member_hashes.items())),
            "license": LICENSE,
            "license_boundary": {
                "assets_and_annotations": LICENSE["id"],
                "repository_code": "repository LICENSE",
                "noncommercial_only": True,
                "share_alike_required": True,
            },
            "timestamp_policy": (
                "Timestamps preserve publisher-declared fixed FPS and ordered JPEG cadence as "
                "(one_based_frame - 1) / FPS, rounded to the nearest integer nanosecond. "
                "Original container PTS is unavailable and is not claimed."
            ),
            "box_policy": (
                "MOT one-based xywh is retained under source_mot and converted to zero-based pixel-edge xyxy. "
                "CVBench bbox_xyxy is clipped only to the visible frame intersection; offscreen source boxes remain "
                "under source_mot."
            ),
            "evaluation_policy": (
                "Marked class 1 is score/eligible; every other row is evaluator ignore. Classes 9, 10, 11, and 13 "
                "are ignore regions. Original classes, marks, identities, boxes, and visibility remain in each row."
            ),
            "mot17_deduplication_policy": (
                "Updated DPM/FRCNN/SDP MOT17 ground-truth and seqinfo copies must agree. Exactly one canonical "
                "official MOT16 pixel sequence is emitted for each MOT17 base video; public detections are excluded."
            ),
            "class_mapping": {str(key): value for key, value in MOT_CLASSES.items()},
            "selected_sequence_ids": [spec.sequence_id for spec in SEQUENCES],
            "sequence_count": len(SEQUENCES),
            "sequences": sequence_results,
            "output_artifact_manifest_sha256": _sha256_bytes(artifact_body),
        }
        manifest = {**payload, "manifest_payload_sha256": _sha256_bytes(_canonical_json(payload))}
        (staging / "corpus-manifest.json").write_bytes(_canonical_json(manifest))
        verify_output(staging)
        staging.rename(output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        for archive in archives.values():
            archive.close()


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "verify"))
    parser.add_argument(
        "--ingest",
        type=Path,
        default=repo_root / ".local-ingest" / "motchallenge",
        help="directory containing the three pinned official archives",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "data" / "motchallenge-training-v1",
        help="external training corpus destination",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        manifest = import_corpus(args.ingest, args.output) if args.action == "import" else verify_output(args.output)
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "sequence_count": manifest["sequence_count"],
                "manifest_payload_sha256": manifest["manifest_payload_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
