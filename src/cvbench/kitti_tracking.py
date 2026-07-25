"""Import user-supplied official KITTI Tracking training data for model training.

This module never downloads KITTI data and never publishes imported annotations
as CVBench benchmark truth. It accepts only the official left-color image and
training-label archive layouts, validates them before committing output, and
copies the original PNG bytes into an ignored local corpus directory.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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

from .protocol import validate_ground_truth

IMAGE_ARCHIVE = "data_tracking_image_2.zip"
LABEL_ARCHIVE = "data_tracking_label_2.zip"
TRAINING_SEQUENCES = tuple(f"{index:04d}" for index in range(21))
FPS = 10
IMPORTER_VERSION = "cvbench.kitti-tracking-import/v1"
MAPPING_POLICY_VERSION = "cvbench.kitti-tracking-car-person/v1"
OFFICIAL_TRACKING_URL = "https://www.cvlibs.net/datasets/kitti/eval_tracking.php"
OFFICIAL_DOWNLOAD_POLICY_URL = "https://www.cvlibs.net/datasets/kitti/user_login.php"
OFFICIAL_DATASET_URL = "https://www.cvlibs.net/datasets/kitti/"

CLASS_POLICY: dict[str, dict[str, Any]] = {
    "Car": {"class_id": "car", "ignore": False, "ignore_region": False, "reason": "score"},
    "Pedestrian": {"class_id": "person", "ignore": False, "ignore_region": False, "reason": "score"},
    "Van": {"class_id": "car", "ignore": True, "ignore_region": False, "reason": "neighboring_class"},
    "Truck": {"class_id": "car", "ignore": True, "ignore_region": False, "reason": "unsupported_class"},
    "Tram": {"class_id": "car", "ignore": True, "ignore_region": False, "reason": "unsupported_class"},
    "Person_sitting": {
        "class_id": "person",
        "ignore": True,
        "ignore_region": False,
        "reason": "neighboring_class",
    },
    "Cyclist": {"class_id": "person", "ignore": True, "ignore_region": False, "reason": "unsupported_class"},
    "Misc": {"class_id": "__ignore__", "ignore": True, "ignore_region": True, "reason": "unsupported_class"},
    "DontCare": {"class_id": "__ignore__", "ignore": True, "ignore_region": True, "reason": "dont_care"},
}

OCCLUSION_POLICY = {
    -1: ("unknown", "not_applicable"),
    0: ("none", "fully_visible"),
    1: ("partial", "partly_occluded"),
    2: ("partial", "largely_occluded"),
    3: ("unknown", "unknown"),
}


class KittiImportError(RuntimeError):
    """The supplied corpus failed closed validation."""


@dataclass(frozen=True)
class SourceArchive:
    path: Path
    sha256: str
    size: int
    archive: zipfile.ZipFile
    members: dict[str, zipfile.ZipInfo]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise KittiImportError(f"{label} is not numeric") from exc
    if not parsed.is_finite():
        raise KittiImportError(f"{label} must be finite")
    return parsed


def _integer(value: str, label: str) -> int:
    parsed = _decimal(value, label)
    if parsed != parsed.to_integral_value():
        raise KittiImportError(f"{label} must be an integer")
    return int(parsed)


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _timestamp_ns(frame_index: int) -> int:
    return frame_index * 1_000_000_000 // FPS


def _png_dimensions(header: bytes, member: str) -> tuple[int, int]:
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise KittiImportError(f"{member} is not a structurally valid PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        raise KittiImportError(f"{member} has invalid PNG dimensions")
    return width, height


def _open_archive(path: Path, expected_sha256: str | None) -> SourceArchive:
    if not path.is_file() or path.is_symlink():
        raise KittiImportError(f"source archive must be a regular non-symlink file: {path}")
    digest = _sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise KittiImportError(f"{path.name} SHA-256 mismatch: expected {expected_sha256.lower()}, got {digest}")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise KittiImportError(f"{path.name} is not a valid ZIP archive") from exc

    infos = archive.infolist()
    names = [info.filename for info in infos]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    folded: dict[str, list[str]] = defaultdict(list)
    unsafe: list[str] = []
    special: list[str] = []
    encrypted: list[str] = []
    for info in infos:
        name = info.filename
        folded[unicodedata.normalize("NFC", name).casefold()].append(name)
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith(("/", "\\"))
            or "\\" in name
            or ".." in pure.parts
            or (pure.parts and ":" in pure.parts[0])
        ):
            unsafe.append(name)
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            special.append(name)
        if info.flag_bits & 0x1:
            encrypted.append(name)
    collisions = [values for values in folded.values() if len(set(values)) > 1]
    if duplicates or collisions or unsafe or special or encrypted:
        archive.close()
        raise KittiImportError(
            f"{path.name} has an unsafe inventory: duplicates={duplicates[:3]} "
            f"case_collisions={collisions[:3]} paths={unsafe[:3]} "
            f"special={special[:3]} encrypted={encrypted[:3]}"
        )
    return SourceArchive(path, digest, path.stat().st_size, archive, {info.filename: info for info in infos})


def _read_member(source: SourceArchive, member: str) -> bytes:
    info = source.members.get(member)
    if info is None or info.is_dir():
        raise KittiImportError(f"{source.path.name} is missing required file {member}")
    try:
        return source.archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise KittiImportError(f"cannot read {member} from {source.path.name}") from exc


def _image_members(source: SourceArchive, sequence: str) -> list[tuple[int, str]]:
    prefix = f"training/image_02/{sequence}/"
    found: list[tuple[int, str]] = []
    invalid: list[str] = []
    for name, info in source.members.items():
        if not name.startswith(prefix) or info.is_dir():
            continue
        leaf = name.removeprefix(prefix)
        if len(leaf) != 10 or not leaf.endswith(".png") or not leaf[:6].isdigit() or "/" in leaf:
            invalid.append(name)
            continue
        found.append((int(leaf[:6]), name))
    if invalid:
        raise KittiImportError(f"{sequence} has unexpected image members: {invalid[:3]}")
    found.sort()
    if not found:
        raise KittiImportError(f"{sequence} has no training images")
    indices = [index for index, _ in found]
    if indices != list(range(len(indices))):
        raise KittiImportError(f"{sequence} image indices must be contiguous from 000000")
    return found


def _parse_labels(
    value: bytes,
    *,
    sequence: str,
    frame_sizes: dict[int, tuple[int, int]],
) -> list[dict[str, Any]]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KittiImportError(f"{sequence} label file is not UTF-8") from exc

    parsed: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    track_classes: dict[int, str] = {}
    ignore_ordinal = 0
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        fields = raw_line.split()
        if len(fields) != 17:
            raise KittiImportError(f"{sequence} label line {line_number} must contain exactly 17 fields")
        frame_index = _integer(fields[0], f"{sequence} line {line_number} frame")
        track_id = _integer(fields[1], f"{sequence} line {line_number} track_id")
        source_class = fields[2]
        policy = CLASS_POLICY.get(source_class)
        if policy is None:
            raise KittiImportError(f"{sequence} label line {line_number} has unknown class {source_class!r}")
        if frame_index not in frame_sizes:
            raise KittiImportError(f"{sequence} label line {line_number} references missing frame {frame_index}")
        if source_class == "DontCare":
            if track_id != -1:
                raise KittiImportError(f"{sequence} DontCare line {line_number} must use track_id -1")
        elif track_id < 0:
            raise KittiImportError(f"{sequence} label line {line_number} has a negative object track_id")
        if track_id >= 0:
            key = (frame_index, track_id)
            if key in seen:
                raise KittiImportError(f"{sequence} has duplicate track_id {track_id} in frame {frame_index}")
            seen.add(key)
            previous = track_classes.setdefault(track_id, source_class)
            if previous != source_class:
                raise KittiImportError(
                    f"{sequence} track_id {track_id} changes class from {previous} to {source_class}"
                )

        truncation = _decimal(fields[3], f"{sequence} line {line_number} truncation")
        occlusion_code = _integer(fields[4], f"{sequence} line {line_number} occlusion")
        if source_class == "DontCare":
            if truncation != Decimal(-1) or occlusion_code != -1:
                raise KittiImportError(
                    f"{sequence} DontCare line {line_number} must use truncation/occlusion sentinels -1"
                )
        elif not Decimal(0) <= truncation <= Decimal(1):
            raise KittiImportError(f"{sequence} label line {line_number} truncation must be between 0 and 1")
        elif occlusion_code not in {0, 1, 2, 3}:
            raise KittiImportError(f"{sequence} label line {line_number} has invalid occlusion {occlusion_code}")
        for field_index, field_name in (
            (5, "alpha"),
            (10, "height"),
            (11, "width"),
            (12, "length"),
            (13, "location_x"),
            (14, "location_y"),
            (15, "location_z"),
            (16, "rotation_y"),
        ):
            _decimal(fields[field_index], f"{sequence} line {line_number} {field_name}")
        box_decimals = [
            _decimal(fields[index], f"{sequence} line {line_number} bbox") for index in range(6, 10)
        ]
        box = [_json_number(number) for number in box_decimals]
        width, height = frame_sizes[frame_index]
        if not (
            Decimal(0) <= box_decimals[0] < box_decimals[2] <= Decimal(width)
            and Decimal(0) <= box_decimals[1] < box_decimals[3] <= Decimal(height)
        ):
            raise KittiImportError(f"{sequence} label line {line_number} has an out-of-frame or empty 2D box")

        if track_id >= 0:
            target_id = f"kitti-{sequence}-track-{track_id:06d}"
        else:
            target_id = f"kitti-{sequence}-ignore-{frame_index:06d}-{ignore_ordinal:04d}"
            ignore_ordinal += 1
        occlusion, source_occlusion = OCCLUSION_POLICY[occlusion_code]
        record: dict[str, Any] = {
            "schema_version": "cvbench.ground-truth/v1",
            "target_id": target_id,
            "sequence_id": f"kitti-tracking-training-{sequence}",
            "source_timestamp_ns": _timestamp_ns(frame_index),
            "on_screen": True,
            "eligible_for_detection": not policy["ignore"],
            "visibility_fraction": None,
            "occlusion": occlusion,
            "truncated": truncation > 0 if truncation >= 0 else False,
            "truncation_fraction": _json_number(truncation) if truncation >= 0 else None,
            "source_truncation": _json_number(truncation),
            "class_id": policy["class_id"],
            "bbox_xyxy": box,
            "ignore": policy["ignore"],
            "ignore_region": policy["ignore_region"],
            "ignore_class_agnostic": policy["ignore_region"],
            "ignore_reason": policy["reason"],
            "source_class": source_class,
            "source_track_id": track_id,
            "source_frame_index": frame_index,
            "source_occlusion": {"code": occlusion_code, "label": source_occlusion},
            "mapping_policy_version": MAPPING_POLICY_VERSION,
        }
        if policy["ignore_region"]:
            record["ignore_region_id"] = target_id
        parsed.append(validate_ground_truth(record))

    frames_by_target: dict[str, list[int]] = defaultdict(list)
    for record in parsed:
        frames_by_target[record["target_id"]].append(record["source_frame_index"])
    for record in parsed:
        frames = frames_by_target[record["target_id"]]
        record["entry_event"] = record["source_frame_index"] == min(frames)
        record["exit_event"] = record["source_frame_index"] == max(frames)
    return sorted(parsed, key=lambda row: (row["source_timestamp_ns"], row["target_id"]))


def _copy_sequence(
    images: SourceArchive,
    labels: SourceArchive,
    sequence: str,
    output_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sequence_root = output_root / "sequences" / sequence
    frames_root = sequence_root / "frames"
    frames_root.mkdir(parents=True)
    source_members: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    frame_sizes: dict[int, tuple[int, int]] = {}
    for frame_index, member in _image_members(images, sequence):
        value = _read_member(images, member)
        width, height = _png_dimensions(value[:24], member)
        if frame_sizes and (width, height) != next(iter(frame_sizes.values())):
            raise KittiImportError(f"{sequence} changes image dimensions within the sequence")
        frame_sizes[frame_index] = (width, height)
        relative_path = f"frames/{frame_index:06d}.png"
        (sequence_root / relative_path).write_bytes(value)
        digest = _sha256_bytes(value)
        source_members.append(
            {"archive": IMAGE_ARCHIVE, "path": member, "bytes": len(value), "sha256": digest}
        )
        frames.append(
            {
                "frame_index": frame_index,
                "source_timestamp_ns": _timestamp_ns(frame_index),
                "width": width,
                "height": height,
                "path": relative_path,
                "payload_encoding": "png",
                "source_sha256": digest,
            }
        )

    label_member = f"training/label_02/{sequence}.txt"
    label_value = _read_member(labels, label_member)
    source_members.append(
        {
            "archive": LABEL_ARCHIVE,
            "path": label_member,
            "bytes": len(label_value),
            "sha256": _sha256_bytes(label_value),
        }
    )
    ground_truth = _parse_labels(label_value, sequence=sequence, frame_sizes=frame_sizes)
    ground_truth_value = b"".join(_canonical_json(row) for row in ground_truth)
    (sequence_root / "ground_truth.jsonl").write_bytes(ground_truth_value)
    scenario = {
        "schema_version": "cvbench.scenario/v1",
        "id": f"kitti-tracking-training-{sequence}",
        "family": "external_training_corpus",
        "sequence_id": f"kitti-tracking-training-{sequence}",
        "usage": "training_only",
        "public_benchmark_truth": False,
        "source_dataset": "KITTI 2D Multi-Object Tracking training set",
        "source_sequence": sequence,
        "source_fps": FPS,
        "timestamp_policy": "frame_index * 1e9 / 10, exact integer nanoseconds",
        "license": "CC-BY-NC-SA-3.0; academic use only",
        "mapping_policy_version": MAPPING_POLICY_VERSION,
        "ontology": ["car", "person"],
        "ground_truth": "ground_truth.jsonl",
        "frames": frames,
    }
    (sequence_root / "scenario.yaml").write_bytes(_canonical_json(scenario))
    return (
        {
            "sequence": sequence,
            "frame_count": len(frames),
            "annotation_count": len(ground_truth),
            "score_annotation_count": sum(not row["ignore"] for row in ground_truth),
            "ignore_annotation_count": sum(row["ignore"] for row in ground_truth),
            "duration_ns": frames[-1]["source_timestamp_ns"],
        },
        source_members,
    )


def _output_inventory(root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "ingest-manifest.json":
            continue
        output.append({"path": relative, "bytes": path.stat().st_size, "sha256": _sha256_file(path)})
    return output


def _inventory_hash(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(f"{entry['path']}\0{entry['bytes']}\0{entry['sha256']}\n".encode())
    return digest.hexdigest()


def import_kitti_tracking(
    input_dir: Path,
    output_dir: Path,
    *,
    sequences: tuple[str, ...] = TRAINING_SEQUENCES,
    accept_license: bool = False,
    expected_images_sha256: str | None = None,
    expected_labels_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate and import selected official KITTI Tracking training sequences."""
    if not accept_license:
        raise KittiImportError("explicit license acceptance is required; pass --accept-license")
    selected = tuple(sorted(set(sequences)))
    if not selected or any(sequence not in TRAINING_SEQUENCES for sequence in selected):
        raise KittiImportError("sequences must be selected from KITTI training IDs 0000 through 0020")
    if output_dir.exists():
        raise KittiImportError(f"output path already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        images = _open_archive(input_dir / IMAGE_ARCHIVE, expected_images_sha256)
        stack.callback(images.archive.close)
        labels = _open_archive(input_dir / LABEL_ARCHIVE, expected_labels_sha256)
        stack.callback(labels.archive.close)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        try:
            summaries: list[dict[str, Any]] = []
            source_members: list[dict[str, Any]] = []
            for sequence in selected:
                summary, members = _copy_sequence(images, labels, sequence, temporary)
                summaries.append(summary)
                source_members.extend(members)
            outputs = _output_inventory(temporary)
            manifest = {
                "schema_version": IMPORTER_VERSION,
                "usage": {
                    "training_only": True,
                    "public_benchmark_truth": False,
                    "test_split_allowed": False,
                    "media_redistribution": False,
                },
                "dataset": {
                    "name": "KITTI 2D Multi-Object Tracking",
                    "split": "training",
                    "official_tracking_url": OFFICIAL_TRACKING_URL,
                    "official_download_policy_url": OFFICIAL_DOWNLOAD_POLICY_URL,
                },
                "license": {
                    "id": "CC-BY-NC-SA-3.0",
                    "notice": (
                        "KITTI publishes its datasets under Creative Commons Attribution-NonCommercial-"
                        "ShareAlike 3.0 and states that the dataset is available for academic use only."
                    ),
                    "official_notice_url": OFFICIAL_DATASET_URL,
                    "accepted_by_operator": True,
                },
                "importer_version": IMPORTER_VERSION,
                "mapping_policy": {
                    "version": MAPPING_POLICY_VERSION,
                    "visibility": (
                        "null: KITTI supplies truncation and ordinal occlusion, not a numeric visible fraction"
                    ),
                    "occlusion": {
                        str(code): {"cvbench": mapped, "source_label": label}
                        for code, (mapped, label) in OCCLUSION_POLICY.items()
                    },
                    "classes": CLASS_POLICY,
                },
                "cadence": {
                    "fps": FPS,
                    "timestamp_origin": "sequence-relative",
                    "timestamp_formula": "frame_index * 100000000 nanoseconds",
                },
                "source_archives": [
                    {
                        "filename": images.path.name,
                        "bytes": images.size,
                        "sha256": images.sha256,
                        "provenance": "operator-supplied official KITTI account download",
                        "expected_sha256_verified": expected_images_sha256 is not None,
                    },
                    {
                        "filename": labels.path.name,
                        "bytes": labels.size,
                        "sha256": labels.sha256,
                        "provenance": "operator-supplied official KITTI account download",
                        "expected_sha256_verified": expected_labels_sha256 is not None,
                    },
                ],
                "source_members": sorted(source_members, key=lambda item: (item["archive"], item["path"])),
                "sequences": summaries,
                "outputs": outputs,
                "output_content_sha256": _inventory_hash(outputs),
            }
            (temporary / "ingest-manifest.json").write_bytes(_canonical_json(manifest))
            temporary.rename(output_dir)
            return manifest
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(".local-ingest/kitti-tracking"))
    parser.add_argument("--output", type=Path, default=Path(".local-ingest/kitti-tracking-cvbench"))
    parser.add_argument(
        "--sequence",
        action="append",
        choices=TRAINING_SEQUENCES,
        help="training sequence to import; repeat as needed (default: all 21)",
    )
    parser.add_argument("--expected-images-sha256")
    parser.add_argument("--expected-labels-sha256")
    parser.add_argument(
        "--accept-license",
        action="store_true",
        help="confirm CC BY-NC-SA 3.0, noncommercial/share-alike, and academic-use terms",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = import_kitti_tracking(
            args.input.resolve(),
            args.output.resolve(),
            sequences=tuple(args.sequence) if args.sequence else TRAINING_SEQUENCES,
            accept_license=args.accept_license,
            expected_images_sha256=args.expected_images_sha256,
            expected_labels_sha256=args.expected_labels_sha256,
        )
    except KittiImportError as exc:
        raise SystemExit(f"KITTI import refused: {exc}") from exc
    print(
        f"Imported {len(manifest['sequences'])} KITTI training sequences; "
        f"content SHA-256 {manifest['output_content_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
