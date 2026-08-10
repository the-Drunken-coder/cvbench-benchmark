#!/usr/bin/env python3
"""Build browser-safe annotated previews from the recovered training corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2
import yaml

OUTPUT_WIDTH = 960
OUTPUT_HEIGHT = 540
OUTPUT_FPS = 5
MAX_ASSET_BYTES = 25 * 1024 * 1024
EXPECTED_SUMMARY = {
    "unique_videos": 5,
    "sample_count": 504,
    "annotation_count": 503,
}
CLIP_COPY = {
    "pexels-18187166-dune": {
        "title": "Huacachina dune ascent",
        "description": (
            "A distant person climbs a dune while scale and contrast change against a textured desert background."
        ),
    },
    "pixabay-112059-dog-road": {
        "title": "Woodland road with person and dog",
        "description": "A person and dog move along a forest road, testing two-class detection at changing scale.",
    },
    "pixabay-145851-forest-bench": {
        "title": "Forest path and bench",
        "description": (
            "A person crosses a fixed forest view with clutter, shadows, and a strong bench-shaped distractor."
        ),
    },
    "pixabay-212474-forest-walk": {
        "title": "Single-person forest walk",
        "description": (
            "A person walks through dense trees; a recurring tree-root false proposal was removed during visual audit."
        ),
    },
    "pixabay-28855-ravine": {
        "title": "Tourists crossing an autumn ravine",
        "description": (
            "Multiple people traverse a wide ravine scene with small targets, occlusion, and uneven terrain."
        ),
    },
}
COLORS = {
    "person": (50, 255, 216),
    "dog": (255, 220, 85),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def assert_corpus(corpus_dir: Path, corpus: dict[str, Any]) -> None:
    summary = corpus.get("summary", {})
    for field, expected in EXPECTED_SUMMARY.items():
        if summary.get(field) != expected:
            raise ValueError(f"corpus {field} must be {expected}, got {summary.get(field)!r}")
    if corpus.get("data_role") != "model_training_only" or corpus.get("evaluation_eligible") is not False:
        raise ValueError("only evaluation-ineligible training corpora can be published here")
    if corpus.get("unknown_is_background") is not False:
        raise ValueError("the recovered corpus must preserve unknown-is-not-background semantics")
    for relative in ["samples.jsonl", "annotations.jsonl", "inventory.sha256"]:
        if not (corpus_dir / relative).is_file():
            raise ValueError(f"missing corpus artifact: {relative}")


def draw_label(
    frame: Any,
    sample: dict[str, Any],
    box: list[float],
    class_id: str,
    confidence: float,
) -> None:
    scale_x = OUTPUT_WIDTH / sample["width"]
    scale_y = OUTPUT_HEIGHT / sample["height"]
    x1, y1, x2, y2 = [
        int(round(value * scale)) for value, scale in zip(box, [scale_x, scale_y, scale_x, scale_y], strict=True)
    ]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(OUTPUT_WIDTH - 1, x2), min(OUTPUT_HEIGHT - 1, y2)
    color = COLORS[class_id]
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
    label = f"{class_id} {confidence:.2f}"
    (text_width, text_height), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.48, 1)
    label_top = max(48, y1 - text_height - baseline - 7)
    cv2.rectangle(
        frame,
        (x1, label_top),
        (min(OUTPUT_WIDTH - 1, x1 + text_width + 8), label_top + text_height + baseline + 7),
        color,
        -1,
    )
    cv2.putText(
        frame,
        label,
        (x1 + 4, label_top + text_height + 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (12, 18, 12),
        1,
        cv2.LINE_AA,
    )


def render_frame(image_path: Path, sample: dict[str, Any], annotations: list[dict[str, Any]], title: str) -> Any:
    frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"cannot decode {image_path}")
    frame = cv2.resize(frame, (OUTPUT_WIDTH, OUTPUT_HEIGHT), interpolation=cv2.INTER_AREA)
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (OUTPUT_WIDTH, 48), (8, 13, 8), -1)
    cv2.addWeighted(overlay, 0.86, frame, 0.14, 0, frame)
    cv2.putText(
        frame,
        "TRAINING-ONLY PSEUDO-LABELS | NOT EVALUATION GROUND TRUTH",
        (14, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (50, 255, 216),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(frame, title, (14, 39), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (245, 247, 240), 1, cv2.LINE_AA)
    for annotation in annotations:
        draw_label(frame, sample, annotation["bbox_xyxy"], annotation["class_id"], annotation["confidence"])
    timestamp = sample["source_timestamp_ns"] / 1_000_000_000
    proposal_state = f"proposals {len(annotations)}" if annotations else "no proposal: unknown, not background"
    footer = f"sample {sample['source_frame_index']} | source {timestamp:.3f}s | {proposal_state}"
    (text_width, _), _ = cv2.getTextSize(footer, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
    cv2.rectangle(frame, (0, OUTPUT_HEIGHT - 26), (min(OUTPUT_WIDTH, text_width + 24), OUTPUT_HEIGHT), (8, 13, 8), -1)
    cv2.putText(frame, footer, (12, OUTPUT_HEIGHT - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (245, 247, 240), 1, cv2.LINE_AA)
    return frame


def encode_preview(
    ffmpeg: str,
    destination: Path,
    poster_destination: Path,
    corpus_dir: Path,
    samples: list[dict[str, Any]],
    annotations: dict[str, list[dict[str, Any]]],
    title: str,
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "bgr24",
        "-video_size",
        f"{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}",
        "-framerate",
        str(OUTPUT_FPS),
        "-i",
        "pipe:0",
        "-an",
        "-map_metadata",
        "-1",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-threads",
        "1",
        "-metadata",
        f"title={title}",
        "-metadata",
        "comment=CVBench training-only pseudo-label preview; not evaluation ground truth",
        str(destination),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for index, sample in enumerate(samples):
            frame = render_frame(corpus_dir / sample["image"], sample, annotations[sample["image"]], title)
            if index == 0 and not cv2.imwrite(
                str(poster_destination),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 82, cv2.IMWRITE_JPEG_OPTIMIZE, 1],
            ):
                raise ValueError(f"cannot write poster {poster_destination}")
            process.stdin.write(frame.tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()
    except Exception:
        process.kill()
        process.wait()
        raise
    if return_code:
        raise RuntimeError(f"ffmpeg failed for {destination.name}: {stderr.strip()}")


def publication_manifest(corpus: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "cvbench.training-media-source/v1",
        "id": "recovered-clean-videos-pseudolabel-previews-v1",
        "title": "Recovered clean-video training previews",
        "description": "Five transformed browser previews rendered from the local pseudo-labeled training corpus.",
        "data_role": corpus["data_role"],
        "evaluation_eligible": corpus["evaluation_eligible"],
        "annotation_scope": corpus["annotation_scope"],
        "annotation_status": corpus["annotation_status"],
        "unknown_is_background": corpus["unknown_is_background"],
        "ontology": corpus["ontology"],
        "labeler": corpus["labeler"],
        "summary": corpus["summary"],
        "derivation": {
            "preview_only": True,
            "clean_source_media_redistributed": False,
            "sample_fps": OUTPUT_FPS,
            "transformation": (
                "Sampled training images resized to 960x540 and encoded as H.264 with burned-in pseudo-label boxes, "
                "confidence, source time, and training-only disclosure."
            ),
        },
        "videos": assets,
    }


def build(corpus_dir: Path, output_dir: Path, ffmpeg: str) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError(f"output directory already exists: {output_dir}")
    corpus = yaml.safe_load((corpus_dir / "corpus.yaml").read_text(encoding="utf-8"))
    assert_corpus(corpus_dir, corpus)
    samples = load_jsonl(corpus_dir / "samples.jsonl")
    raw_annotations = load_jsonl(corpus_dir / "annotations.jsonl")
    samples_by_clip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    annotations_by_image: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        samples_by_clip[sample["clip_id"]].append(sample)
    for annotation in raw_annotations:
        annotations_by_image[annotation["image"]].append(annotation)
    for clip_samples in samples_by_clip.values():
        clip_samples.sort(key=lambda item: item["source_timestamp_ns"])

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        preview_dir = temporary / "previews"
        poster_dir = temporary / "posters"
        preview_dir.mkdir()
        poster_dir.mkdir()
        sources = {source["id"]: source for source in corpus["sources"]}
        assets = []
        for clip_id in sorted(samples_by_clip):
            if clip_id not in CLIP_COPY or clip_id not in sources:
                raise ValueError(f"undeclared clip: {clip_id}")
            clip_samples = samples_by_clip[clip_id]
            source = sources[clip_id]
            destination = preview_dir / f"{clip_id}.mp4"
            poster = poster_dir / f"{clip_id}.jpg"
            encode_preview(
                ffmpeg,
                destination,
                poster,
                corpus_dir,
                clip_samples,
                annotations_by_image,
                CLIP_COPY[clip_id]["title"],
            )
            size = destination.stat().st_size
            if size > MAX_ASSET_BYTES:
                raise ValueError(f"preview exceeds Cloudflare's 25 MiB static-asset limit: {destination.name}")
            clip_annotations = [
                annotation for sample in clip_samples for annotation in annotations_by_image[sample["image"]]
            ]
            assets.append(
                {
                    "id": clip_id,
                    **CLIP_COPY[clip_id],
                    "creator": source["creator"],
                    "source_url": source["source_url"],
                    "source_sha256": source["sha256"],
                    "license_name": source["license_name"],
                    "license_url": source["license_url"],
                    "split": source["split"],
                    "sample_count": len(clip_samples),
                    "annotation_count": len(clip_annotations),
                    "class_counts": dict(
                        sorted(Counter(annotation["class_id"] for annotation in clip_annotations).items())
                    ),
                    "preview_path": f"previews/{destination.name}",
                    "preview_sha256": sha256_file(destination),
                    "preview_bytes": size,
                    "preview_width": OUTPUT_WIDTH,
                    "preview_height": OUTPUT_HEIGHT,
                    "preview_fps": OUTPUT_FPS,
                    "preview_duration_seconds": len(clip_samples) / OUTPUT_FPS,
                    "poster_path": f"posters/{poster.name}",
                    "poster_sha256": sha256_file(poster),
                    "poster_bytes": poster.stat().st_size,
                }
            )
        manifest = publication_manifest(corpus, assets)
        (temporary / "publication.json").write_text(
            f"{json.dumps(manifest, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        temporary.rename(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify(output_dir: Path) -> dict[str, Any]:
    manifest = json.loads((output_dir / "publication.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "cvbench.training-media-source/v1":
        raise ValueError("invalid publication schema")
    if manifest.get("data_role") != "model_training_only" or manifest.get("evaluation_eligible") is not False:
        raise ValueError("publication boundary is not training-only")
    if len(manifest.get("videos", [])) != 5:
        raise ValueError("publication must contain five previews")
    for video in manifest["videos"]:
        asset = output_dir / video["preview_path"]
        if (
            not asset.is_file()
            or asset.stat().st_size != video["preview_bytes"]
            or sha256_file(asset) != video["preview_sha256"]
        ):
            raise ValueError(f"preview verification failed: {video['id']}")
        if asset.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError(f"preview exceeds Cloudflare limit: {video['id']}")
        poster = output_dir / video["poster_path"]
        if (
            not poster.is_file()
            or poster.stat().st_size != video["poster_bytes"]
            or sha256_file(poster) != video["poster_sha256"]
        ):
            raise ValueError(f"poster verification failed: {video['id']}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, default=Path(".local-ingest/recovered-videos-v1"))
    parser.add_argument("--output-dir", type=Path, default=Path("training-media/recovered-videos-v1"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--verify-only", action="store_true")
    arguments = parser.parse_args()
    result = (
        verify(arguments.output_dir)
        if arguments.verify_only
        else build(arguments.corpus_dir, arguments.output_dir, arguments.ffmpeg)
    )
    print(
        json.dumps(
            {"videos": len(result["videos"]), "bytes": sum(video["preview_bytes"] for video in result["videos"])}
        )
    )


if __name__ == "__main__":
    main()
