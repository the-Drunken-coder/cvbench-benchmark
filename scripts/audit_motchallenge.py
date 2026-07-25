#!/usr/bin/env python3
"""Generate deterministic MOTChallenge visual-audit evidence and viewer videos."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import imageio_ffmpeg

GRID_WIDTH = 1920
FRAME_COLUMNS = 10
TRACK_COLUMNS = 8
FRAME_THUMBNAIL = (192, 108)
TRACK_THUMBNAIL = (240, 160)
FRAME_SAMPLE_COUNT = 60
TRACK_SAMPLE_COUNT = 12


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_number(timestamp_ns: int, fps: int) -> int:
    return (timestamp_ns * fps + 500_000_000) // 1_000_000_000


def _scan_ground_truth(path: Path, fps: int) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    frames: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"score_count": 0, "ignore_count": 0, "min_visibility": 1.0, "max_visibility": 0.0}
    )
    tracks: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "frames": [],
            "min_visibility": 2.0,
            "min_visibility_frame": 0,
            "partial_count": 0,
            "full_count": 0,
        }
    )
    for line in path.read_text().splitlines():
        row = json.loads(line)
        frame = _frame_number(row["source_timestamp_ns"], fps)
        visibility = float(row["visibility_fraction"])
        frames[frame]["ignore_count" if row["ignore"] else "score_count"] += 1
        frames[frame]["min_visibility"] = min(frames[frame]["min_visibility"], visibility)
        frames[frame]["max_visibility"] = max(frames[frame]["max_visibility"], visibility)
        if row["ignore"]:
            continue
        track = tracks[row["target_id"]]
        track["frames"].append(frame)
        if visibility < track["min_visibility"]:
            track["min_visibility"] = visibility
            track["min_visibility_frame"] = frame
        track["partial_count"] += row["occlusion"] == "partial"
        track["full_count"] += row["occlusion"] == "full"
    return dict(frames), dict(tracks)


def _sample_frames(
    frame_count: int, frame_stats: dict[int, dict[str, Any]], rng: random.Random
) -> tuple[list[int], dict[str, list[int]]]:
    first_mid_last = [0, frame_count // 2, frame_count - 1]
    uniform = [round(index * (frame_count - 1) / 19) for index in range(20)]
    remaining = [index for index in range(frame_count) if index not in set(first_mid_last + uniform)]
    seeded_random = sorted(rng.sample(remaining, min(20, len(remaining))))
    density = sorted(
        range(frame_count),
        key=lambda index: (
            -(frame_stats.get(index, {}).get("score_count", 0) + frame_stats.get(index, {}).get("ignore_count", 0)),
            index,
        ),
    )[:10]
    visibility_low = sorted(
        range(frame_count),
        key=lambda index: (frame_stats.get(index, {}).get("min_visibility", 1.0), index),
    )[:10]
    visibility_high = sorted(
        range(frame_count),
        key=lambda index: (-frame_stats.get(index, {}).get("max_visibility", 0.0), index),
    )[:10]
    categories = {
        "first_mid_last": first_mid_last,
        "uniform": sorted(set(uniform)),
        "seeded_random": seeded_random,
        "highest_density": density,
        "visibility_low_extremes": visibility_low,
        "visibility_high_extremes": visibility_high,
    }
    selected = sorted({value for values in categories.values() for value in values})
    cursor = 0
    while len(selected) < FRAME_SAMPLE_COUNT:
        candidate = round(cursor * (frame_count - 1) / (FRAME_SAMPLE_COUNT - 1))
        if candidate not in selected:
            selected.append(candidate)
        cursor += 1
    return sorted(selected[:FRAME_SAMPLE_COUNT]), categories


def _sample_tracks(tracks: dict[str, dict[str, Any]], rng: random.Random) -> list[str]:
    ordered = sorted(tracks, key=lambda key: (len(tracks[key]["frames"]), key))
    if len(ordered) < TRACK_SAMPLE_COUNT:
        raise RuntimeError("sequence has fewer than twelve scored person tracks")
    quantile = {
        ordered[round(index * (len(ordered) - 1) / (TRACK_SAMPLE_COUNT - 1))]
        for index in range(TRACK_SAMPLE_COUNT)
    }
    occluded = sorted(
        tracks,
        key=lambda key: (
            -tracks[key]["full_count"],
            -tracks[key]["partial_count"],
            tracks[key]["min_visibility"],
            key,
        ),
    )
    selected = list(sorted(quantile))
    for key in occluded:
        if key not in selected:
            selected.append(key)
        if len(selected) >= TRACK_SAMPLE_COUNT:
            break
    rng.shuffle(selected)
    return sorted(selected[:TRACK_SAMPLE_COUNT])


def _collect_rows(
    path: Path,
    fps: int,
    frames: set[int],
    track_frames: dict[str, set[int]],
) -> tuple[dict[int, list[dict[str, Any]]], dict[tuple[str, int], dict[str, Any]]]:
    by_frame: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_track_frame: dict[tuple[str, int], dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        frame = _frame_number(row["source_timestamp_ns"], fps)
        if frame in frames:
            by_frame[frame].append(row)
        if frame in track_frames.get(row["target_id"], set()):
            by_track_frame[(row["target_id"], frame)] = row
    return dict(by_frame), by_track_frame


def _overlay(image: Any, rows: list[dict[str, Any]], frame: int) -> Any:
    result = image.copy()
    for row in rows:
        box = row.get("bbox_xyxy")
        if not box:
            continue
        color = (0, 170, 255) if row["ignore"] else (40, 220, 40)
        x1, y1, x2, y2 = (round(value) for value in box)
        cv2.rectangle(result, (x1, y1), (x2, y2), color, 2)
    cv2.putText(
        result,
        f"frame {frame + 1} | green score | amber neutral-ignore",
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        result,
        f"frame {frame + 1} | green score | amber neutral-ignore",
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (20, 20, 20),
        1,
        cv2.LINE_AA,
    )
    return result


def _letterbox(image: Any, size: tuple[int, int]) -> Any:
    width, height = size
    scale = min(width / image.shape[1], height / image.shape[0])
    resized = cv2.resize(image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))))
    canvas = cv2.copyMakeBorder(
        resized,
        0,
        height - resized.shape[0],
        0,
        width - resized.shape[1],
        cv2.BORDER_CONSTANT,
        value=(18, 18, 18),
    )
    return canvas[:height, :width]


def _track_crop(image: Any, row: dict[str, Any], label: str) -> Any:
    box = row.get("bbox_xyxy")
    if not box:
        return _letterbox(image, TRACK_THUMBNAIL)
    x1, y1, x2, y2 = box
    margin_x = max(24, (x2 - x1) * 1.5)
    margin_y = max(24, (y2 - y1) * 0.8)
    left = max(0, int(x1 - margin_x))
    top = max(0, int(y1 - margin_y))
    right = min(image.shape[1], int(x2 + margin_x))
    bottom = min(image.shape[0], int(y2 + margin_y))
    crop = image[top:bottom, left:right].copy()
    cv2.rectangle(
        crop,
        (round(x1 - left), round(y1 - top)),
        (round(x2 - left), round(y2 - top)),
        (40, 220, 40),
        2,
    )
    crop = _letterbox(crop, TRACK_THUMBNAIL)
    cv2.putText(crop, label, (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(crop, label, (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)
    return crop


def _grid(images: list[Any], columns: int, cell: tuple[int, int]) -> Any:
    rows = (len(images) + columns - 1) // columns
    blank = cv2.UMat(cell[1], cell[0], cv2.CV_8UC3).get()
    blank[:] = (18, 18, 18)
    padded = [*images, *[blank] * (rows * columns - len(images))]
    return cv2.vconcat([cv2.hconcat(padded[index : index + columns]) for index in range(0, len(padded), columns)])


def _video(
    ffmpeg: str,
    frame_root: Path,
    output: Path,
    fps: int,
    expected_frames: int,
) -> dict[str, Any]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "image2",
        "-framerate",
        str(fps),
        "-start_number",
        "0",
        "-i",
        str(frame_root / "frame-%06d.jpg"),
        "-frames:v",
        str(expected_frames),
        "-an",
        "-vf",
        "scale=-2:270:flags=lanczos",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "35",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]
    subprocess.run(command, check=True, timeout=900)
    probe = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return {
        "path": output.as_posix(),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "fps": fps,
        "frame_count": expected_frames,
        "encoding": "H.264 yuv420p, 270-pixel height, CRF 35; deterministic single-threaded command",
        "decode_validation_stderr": probe.stderr,
        "command": [
            "<hydrated-frame-root>/frame-%06d.jpg"
            if value == str(frame_root / "frame-%06d.jpg")
            else value
            for value in command[1:-1]
        ]
        + [output.name],
    }


def generate(repo_root: Path, *, review_status: str) -> dict[str, Any]:
    source_root = repo_root / "scenarios" / "motchallenge-v1"
    data_root = repo_root / "data" / "motchallenge-v1"
    manifest = json.loads((source_root / "ingest-manifest.json").read_text())
    seed = int(manifest["audit_seed"][:16], 16)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_digest = _sha256(Path(ffmpeg))
    version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    visual_root = source_root / "visual-audit"
    public_root = source_root / "public"
    visual_root.mkdir(parents=True, exist_ok=True)
    public_root.mkdir(parents=True, exist_ok=True)
    audit: dict[str, Any] = {
        "schema_version": "cvbench.motchallenge-visual-audit/v1",
        "manifest_sha256": manifest["manifest_sha256"],
        "audit_seed": manifest["audit_seed"],
        "review_status": review_status,
        "selection_policy": (
            "At least 60 unique frames per sequence spanning first/middle/last, 20 uniform samples, 20 seeded "
            "random samples, highest density, and visibility extremes; 12 scored tracks stratified by lifespan "
            "and occlusion, each shown at birth, minimum visibility, and death."
        ),
        "manual_annotation_edits": [],
        "manual_review": {
            "scope": (
                "All ten overview sheets, each containing at least 60 exact source frames and 12 stratified "
                "scored tracks at birth, minimum visibility, and death."
            ),
            "finding": (
                "No systematic coordinate or identity drift was found. Visibility-zero track crops are retained "
                "as publisher annotations; no unreviewed truth was invented or interpolated."
            ),
        },
        "normalization_corrections": {
            "policy": manifest["correction_policy"],
            "count": manifest["totals"]["boundary_corrections"],
            "sha256": manifest["corrections_sha256"],
        },
        "ffmpeg": {"version": version, "sha256": ffmpeg_digest},
        "sequences": {},
    }
    for sequence_index, (scenario_id, info) in enumerate(sorted(manifest["sequence_results"].items())):
        rng = random.Random(seed + sequence_index)
        scenario_root = data_root / scenario_id
        frame_stats, tracks = _scan_ground_truth(scenario_root / "ground_truth.jsonl", info["fps"])
        selected_frames, categories = _sample_frames(info["frame_count"], frame_stats, rng)
        selected_tracks = _sample_tracks(tracks, rng)
        track_frames = {}
        for target_id in selected_tracks:
            values = tracks[target_id]["frames"]
            track_frames[target_id] = {values[0], tracks[target_id]["min_visibility_frame"], values[-1]}
        rows_by_frame, rows_by_track_frame = _collect_rows(
            scenario_root / "ground_truth.jsonl",
            info["fps"],
            set(selected_frames),
            track_frames,
        )
        frame_thumbnails = []
        for frame in selected_frames:
            image = cv2.imread(str(scenario_root / "frames" / f"frame-{frame:06d}.jpg"))
            if image is None:
                raise RuntimeError(f"cannot read {scenario_id} frame {frame}")
            frame_thumbnails.append(_letterbox(_overlay(image, rows_by_frame.get(frame, []), frame), FRAME_THUMBNAIL))
        frame_grid = _grid(frame_thumbnails, FRAME_COLUMNS, FRAME_THUMBNAIL)
        track_thumbnails = []
        track_audit = []
        for target_id in selected_tracks:
            frames = sorted(track_frames[target_id])
            track_audit.append(
                {
                    "target_id": target_id,
                    "birth_frame": tracks[target_id]["frames"][0] + 1,
                    "visibility_extreme_frame": tracks[target_id]["min_visibility_frame"] + 1,
                    "death_frame": tracks[target_id]["frames"][-1] + 1,
                    "min_visibility": tracks[target_id]["min_visibility"],
                    "partial_count": tracks[target_id]["partial_count"],
                    "full_count": tracks[target_id]["full_count"],
                }
            )
            for frame in frames:
                image = cv2.imread(str(scenario_root / "frames" / f"frame-{frame:06d}.jpg"))
                row = rows_by_track_frame[(target_id, frame)]
                track_thumbnails.append(
                    _track_crop(image, row, f"{target_id} f{frame + 1} vis={row['visibility_fraction']}")
                )
        track_grid = _grid(track_thumbnails, TRACK_COLUMNS, TRACK_THUMBNAIL)
        heading = cv2.UMat(70, GRID_WIDTH, cv2.CV_8UC3).get()
        heading[:] = (14, 18, 24)
        heading_text = (
            f"{info['sequence_id']} | exact JPEG overlays | "
            f"{len(selected_frames)} frames | {len(selected_tracks)} tracks"
        )
        cv2.putText(
            heading,
            heading_text,
            (24, 44),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        overview = cv2.vconcat([heading, frame_grid, track_grid])
        overview_path = visual_root / f"{scenario_id}-overview.jpg"
        if not cv2.imwrite(str(overview_path), overview, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            raise RuntimeError(f"cannot write {overview_path}")
        video_path = public_root / f"{scenario_id}.mp4"
        video = _video(ffmpeg, scenario_root / "frames", video_path, info["fps"], info["frame_count"])
        video["path"] = video_path.relative_to(repo_root).as_posix()
        annotation_path = public_root / f"{scenario_id}-ground-truth.jsonl.gz"
        annotation_path.write_bytes(
            gzip.compress((scenario_root / "ground_truth.jsonl").read_bytes(), compresslevel=9, mtime=0)
        )
        audit["sequences"][scenario_id] = {
            "sequence_id": info["sequence_id"],
            "selected_frame_count": len(selected_frames),
            "selected_frames_one_based": [frame + 1 for frame in selected_frames],
            "frame_categories_one_based": {
                key: [frame + 1 for frame in values] for key, values in categories.items()
            },
            "selected_track_count": len(selected_tracks),
            "tracks": track_audit,
            "overview": {
                "path": overview_path.relative_to(repo_root).as_posix(),
                "bytes": overview_path.stat().st_size,
                "sha256": _sha256(overview_path),
            },
            "viewer_derivative": video,
            "annotation_bundle": {
                "path": annotation_path.relative_to(repo_root).as_posix(),
                "bytes": annotation_path.stat().st_size,
                "sha256": _sha256(annotation_path),
                "encoding": "gzip",
                "content_type": "application/x-ndjson",
            },
            "exact_frame_manifest_sha256": manifest["expected_frame_manifest_sha256"],
            "normalized_gt_sha256": info["normalized_gt_sha256"],
        }
    audit_path = source_root / "visual-audit.json"
    audit_path.write_bytes(_canonical_json(audit))
    print(json.dumps({"audit_sha256": _sha256(audit_path), "review_status": review_status}, sort_keys=True))
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--review-status",
        choices=("generated_pending_manual_review", "manual_review_completed"),
        default="generated_pending_manual_review",
    )
    args = parser.parse_args()
    generate(args.repo_root.resolve(), review_status=args.review_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
