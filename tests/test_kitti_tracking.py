import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from cvbench.errors import ConfigurationError
from cvbench.kitti_tracking import (
    IMAGE_ARCHIVE,
    LABEL_ARCHIVE,
    KittiImportError,
    import_kitti_tracking,
)
from cvbench.runner import _load_unique_scenarios
from cvbench.scenario import load_scenario


def _png(width: int = 64, height: int = 48, value: int = 0) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    pixels = b"".join(b"\0" + bytes([value, value, value]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value)


def _fixture(input_dir: Path, labels: str, *, frame_count: int = 2) -> None:
    input_dir.mkdir()
    _zip(
        input_dir / IMAGE_ARCHIVE,
        {
            f"training/image_02/0000/{index:06d}.png": _png(value=index)
            for index in range(frame_count)
        },
    )
    _zip(input_dir / LABEL_ARCHIVE, {"training/label_02/0000.txt": labels.encode()})


def _line(
    frame: int,
    track: int,
    source_class: str,
    *,
    truncation: str = "0",
    occlusion: int = 0,
) -> str:
    return (
        f"{frame} {track} {source_class} {truncation} {occlusion} 0 "
        "1 2 20 30 1 1 1 0 0 0 0"
    )


def test_import_is_deterministic_and_preserves_kitti_training_semantics(tmp_path: Path) -> None:
    labels = "\n".join(
        [
            _line(0, 1, "Car", truncation="0.25", occlusion=1),
            _line(1, 1, "Car", occlusion=2),
            _line(0, 2, "Pedestrian"),
            _line(0, 3, "Van"),
            _line(0, 4, "Person_sitting"),
            _line(0, 5, "Truck"),
            _line(0, 6, "Cyclist"),
            _line(0, 7, "Tram"),
            _line(0, 8, "Misc"),
            _line(0, -1, "DontCare", truncation="-1", occlusion=-1),
        ]
    )
    input_dir = tmp_path / "input"
    _fixture(input_dir, labels)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_manifest = import_kitti_tracking(
        input_dir, first, sequences=("0000",), accept_license=True
    )
    second_manifest = import_kitti_tracking(
        input_dir, second, sequences=("0000",), accept_license=True
    )

    assert (first / "ingest-manifest.json").read_bytes() == (
        second / "ingest-manifest.json"
    ).read_bytes()
    assert first_manifest["output_content_sha256"] == second_manifest["output_content_sha256"]
    assert first_manifest["usage"] == {
        "training_only": True,
        "public_benchmark_truth": False,
        "test_split_allowed": False,
        "media_redistribution": False,
    }
    assert {item["filename"] for item in first_manifest["source_archives"]} == {
        IMAGE_ARCHIVE,
        LABEL_ARCHIVE,
    }
    assert all(not item["expected_sha256_verified"] for item in first_manifest["source_archives"])
    assert all(len(item["sha256"]) == 64 for item in first_manifest["source_members"])
    assert all(len(item["sha256"]) == 64 for item in first_manifest["outputs"])

    scenario = load_scenario(first / "sequences/0000/scenario.yaml")
    assert scenario.training_only is True
    assert [frame.relative_timestamp_ns for frame in scenario.frames] == [0, 100_000_000]
    assert all(frame.payload_encoding == "png" for frame in scenario.frames)
    assert scenario.frames[0].path.read_bytes() == _png(value=0)

    rows = [
        json.loads(line)
        for line in (first / "sequences/0000/ground_truth.jsonl").read_text().splitlines()
    ]
    scored = [row for row in rows if not row["ignore"]]
    assert {(row["source_class"], row["class_id"]) for row in scored} == {
        ("Car", "car"),
        ("Pedestrian", "person"),
    }
    assert all(row["visibility_fraction"] is None for row in rows)
    car_rows = [row for row in rows if row["source_class"] == "Car"]
    assert [row["source_occlusion"]["code"] for row in car_rows] == [1, 2]
    assert [row["occlusion"] for row in car_rows] == ["partial", "partial"]
    assert car_rows[0]["truncation_fraction"] == 0.25
    assert car_rows[0]["target_id"] == car_rows[1]["target_id"]
    assert car_rows[0]["entry_event"] is True
    assert car_rows[1]["exit_event"] is True
    ignored = {row["source_class"]: row for row in rows if row["ignore"]}
    assert ignored["Van"]["class_id"] == "car"
    assert ignored["Person_sitting"]["class_id"] == "person"
    assert ignored["Misc"]["ignore_region"] is True
    assert ignored["DontCare"]["ignore_region"] is True
    assert ignored["DontCare"]["ignore_class_agnostic"] is True
    assert ignored["DontCare"]["source_truncation"] == -1
    assert ignored["DontCare"]["truncation_fraction"] is None
    assert ignored["DontCare"]["source_occlusion"] == {
        "code": -1,
        "label": "not_applicable",
    }
    with pytest.raises(ConfigurationError, match="training-only scenario"):
        _load_unique_scenarios(
            (first / "sequences/0000/scenario.yaml",),
            "run-training-refused",
        )


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (_line(0, 1, "Alien"), "unknown class"),
        (_line(2, 1, "Car"), "references missing frame"),
        ("0 1 Car 0 0 0 1 2 100 30 1 1 1 0 0 0 0", "out-of-frame"),
        (_line(0, -1, "Car"), "negative object track_id"),
        (_line(0, 1, "DontCare"), "must use track_id -1"),
    ],
)
def test_invalid_inputs_fail_closed_without_partial_output(
    tmp_path: Path, labels: str, message: str
) -> None:
    input_dir = tmp_path / "input"
    _fixture(input_dir, labels, frame_count=1)
    output = tmp_path / "output"

    with pytest.raises(KittiImportError, match=message):
        import_kitti_tracking(input_dir, output, sequences=("0000",), accept_license=True)

    assert not output.exists()


def test_import_requires_explicit_license_acceptance_and_can_pin_source_hashes(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    _fixture(input_dir, _line(0, 1, "Car"), frame_count=1)

    with pytest.raises(KittiImportError, match="license acceptance"):
        import_kitti_tracking(input_dir, tmp_path / "license", sequences=("0000",))
    with pytest.raises(KittiImportError, match="SHA-256 mismatch"):
        import_kitti_tracking(
            input_dir,
            tmp_path / "hash",
            sequences=("0000",),
            accept_license=True,
            expected_images_sha256="0" * 64,
        )


def test_import_refuses_non_training_sequence_ids(tmp_path: Path) -> None:
    with pytest.raises(KittiImportError, match="0000 through 0020"):
        import_kitti_tracking(
            tmp_path,
            tmp_path / "output",
            sequences=("0021",),
            accept_license=True,
        )
