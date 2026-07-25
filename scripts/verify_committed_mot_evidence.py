#!/usr/bin/env python3
"""Verify committed native-Linux MOT evidence without hydrating MOT media."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

try:
    from scripts.verify_ci_evidence import _assert_safe
except ModuleNotFoundError:  # Direct script execution puts scripts/ on sys.path.
    from verify_ci_evidence import _assert_safe

MOT_IDS = [
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
COMBINED_IDS = {
    "synthetic-acquisition",
    "synthetic-false-detection",
    "synthetic-multi-target-identity",
    "synthetic-multi-target-pair",
    "synthetic-occlusion-gap-1000ms",
    "synthetic-occlusion-gap-100ms",
    "synthetic-occlusion-gap-2000ms",
    "synthetic-occlusion-gap-250ms",
    "synthetic-occlusion-gap-500ms",
    "synthetic-occlusion-reacquisition",
    "synthetic-resource-stress",
    "synthetic-track-id-churn",
    "synthetic-visible-retention",
    "rvmot-a1c9",
    "rvmot-b7e2",
    "rvmot-c4f6",
    *MOT_IDS,
}
OFFICIAL_ARCHIVES = {
    "MOT16.zip": {
        "bytes": 1_954_509_127,
        "retrieval_utc": "2026-07-24T01:45:04Z",
        "sha256": "b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b",
    },
    "MOT17Labels.zip": {
        "bytes": 10_107_022,
        "retrieval_utc": "2026-07-24T01:45:04Z",
        "sha256": "0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9",
    },
    "MOT20.zip": {
        "bytes": 5_028_926_248,
        "retrieval_utc": "2026-07-24T01:45:07Z",
        "sha256": "ebcf0e3d44e4f50b5357d24817e5db485d777633d1b8ca9e8380d1c8437dbdd7",
    },
}
LICENSE_LEGALCODE_SHA256 = "8812f83442fd0eca14eb0208988e190fdcbfebec58fa5459d3218edfdfdc5a32"
EVIDENCE_FILES = {
    "evidence/motchallenge-v1/corpora/motchallenge-v1-artifacts.sha256",
    "evidence/motchallenge-v1/corpora/real-video-v2-artifacts.sha256",
    "evidence/motchallenge-v1/corpus-manifests.json",
    "evidence/motchallenge-v1/combined/20260724T075153Z-d36caca2/report.json",
    "evidence/motchallenge-v1/combined/20260724T075153Z-d36caca2/resources.csv",
    "evidence/motchallenge-v1/isolated/20260724T073004Z-68fc08ee/report.json",
    "evidence/motchallenge-v1/isolated/20260724T073004Z-68fc08ee/resources.csv",
    "scenario-catalog/evidence/motchallenge-v1.json",
    "scenarios/motchallenge-v1/corrections.jsonl",
    "scenarios/motchallenge-v1/expected-frame-sha256.txt",
    "scenarios/motchallenge-v1/ingest-manifest.json",
    "scenarios/motchallenge-v1/normalized-ground-truth-sha256.txt",
    "scenarios/motchallenge-v1/visual-audit.json",
}
RESOURCE_FIELDS = [
    "elapsed_ms",
    "cpu_percent",
    "cpu_time_seconds",
    "memory_bytes",
    "disk_read_bytes",
    "disk_write_bytes",
    "network_rx_bytes",
    "network_tx_bytes",
    "process_count",
    "thread_count",
    "gpu_percent",
    "vram_bytes",
    "phase",
    "scenario",
    "target_count",
    "fault_injection",
]


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _hash_manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        digest, separator, relative = line.partition("  ")
        relative_path = Path(relative)
        if (
            separator != "  "
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in result
        ):
            raise RuntimeError(f"invalid hash manifest entry at {path}:{line_number}")
        result[relative] = digest
    if not result:
        raise RuntimeError(f"empty hash manifest: {path}")
    return result


def verify_hash_bound_files(repo_root: Path, manifest: Path, required: set[str]) -> None:
    declared = _hash_manifest(manifest)
    if set(declared) != required:
        raise RuntimeError("committed MOT evidence manifest omits or adds an evidence file")
    for relative, expected in declared.items():
        path = repo_root / relative
        if path.is_symlink() or not path.is_file() or _digest(path) != expected:
            raise RuntimeError(f"committed MOT evidence hash mismatch: {relative}")


def _corpus_fingerprints(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if set(value) != {"schema_version", "corpora"}:
        raise RuntimeError("corpus fingerprint document has undeclared fields")
    if value["schema_version"] != "cvbench.corpus-manifest-fingerprints/v1":
        raise RuntimeError("corpus fingerprint document has the wrong schema")
    corpora = value["corpora"]
    if not isinstance(corpora, dict) or set(corpora) != {"motchallenge-v1", "real-video-v2"}:
        raise RuntimeError("corpus fingerprint document must declare both exact corpora")
    for corpus in corpora.values():
        if not isinstance(corpus, dict) or set(corpus) != {
            "artifact_manifest_entries",
            "artifact_manifest_path",
            "artifact_manifest_sha256",
        }:
            raise RuntimeError("corpus fingerprint entry has the wrong schema")
        if not isinstance(corpus["artifact_manifest_entries"], int) or corpus["artifact_manifest_entries"] <= 0:
            raise RuntimeError("corpus fingerprint entry count is invalid")
        digest = corpus["artifact_manifest_sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("corpus fingerprint digest is invalid")
        manifest_path = Path(corpus["artifact_manifest_path"])
        if manifest_path.is_absolute() or ".." in manifest_path.parts:
            raise RuntimeError("corpus fingerprint path is invalid")
    return corpora


def verify_corpus_manifests(repo_root: Path, real_video_manifest: Path) -> dict[str, str]:
    corpora = _corpus_fingerprints(repo_root / "evidence/motchallenge-v1/corpus-manifests.json")
    real = _hash_manifest(real_video_manifest)
    real_contract = corpora["real-video-v2"]
    committed_real_path = repo_root / real_contract["artifact_manifest_path"]
    committed_real = _hash_manifest(committed_real_path)
    if (
        real != committed_real
        or len(real) != real_contract["artifact_manifest_entries"]
        or _digest(committed_real_path) != real_contract["artifact_manifest_sha256"]
    ):
        raise RuntimeError("real-video artifact manifest differs from committed evidence")

    frame_manifest = repo_root / "scenarios/motchallenge-v1/expected-frame-sha256.txt"
    truth_manifest = repo_root / "scenarios/motchallenge-v1/normalized-ground-truth-sha256.txt"
    mot = _hash_manifest(frame_manifest)
    truth = _hash_manifest(truth_manifest)
    if set(mot) & set(truth):
        raise RuntimeError("MOT frame and ground-truth manifests overlap")
    mot.update(truth)
    canonical = "".join(f"{mot[relative]}  {relative}\n" for relative in sorted(mot)).encode()
    mot_contract = corpora["motchallenge-v1"]
    committed_mot_path = repo_root / mot_contract["artifact_manifest_path"]
    committed_mot = _hash_manifest(committed_mot_path)
    if (
        mot != committed_mot
        or len(mot) != mot_contract["artifact_manifest_entries"]
        or hashlib.sha256(canonical).hexdigest() != mot_contract["artifact_manifest_sha256"]
        or _digest(committed_mot_path) != mot_contract["artifact_manifest_sha256"]
    ):
        raise RuntimeError("MOT artifact manifest differs from committed evidence")
    return committed_mot


def _benchmark_scenarios(repo_root: Path, manifest_name: str) -> list[Path]:
    benchmark = yaml.safe_load((repo_root / manifest_name).read_text())
    root = (repo_root / manifest_name).parent
    return [(root / relative).resolve() for relative in benchmark["scenarios"]]


def _scenario_fingerprints(
    repo_root: Path,
    benchmark_name: str,
    mot_hashes: dict[str, str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for manifest_path in _benchmark_scenarios(repo_root, benchmark_name):
        manifest = yaml.safe_load(manifest_path.read_text())
        scenario_id = manifest["id"]
        ground_truth_path = (manifest_path.parent / manifest["ground_truth"]).resolve()
        if scenario_id in MOT_IDS:
            ground_truth_hash = mot_hashes[f"{scenario_id}/ground_truth.jsonl"]
        else:
            ground_truth_hash = _digest(ground_truth_path)
        frames = []
        for frame in manifest["frames"]:
            if scenario_id in MOT_IDS:
                frame_hash = mot_hashes[
                    f"{scenario_id}/frames/frame-{frame['frame_index']:06d}.jpg"
                ]
            else:
                frame_hash = _digest((manifest_path.parent / frame["path"]).resolve())
            frames.append(
                {
                    "frame_index": frame["frame_index"],
                    "sha256": frame_hash,
                    "source_timestamp_ns": frame["source_timestamp_ns"],
                }
            )
        result[scenario_id] = {
            "frame_sha256": frames,
            "ground_truth_sha256": ground_truth_hash,
            "id": scenario_id,
            "manifest_sha256": _digest(manifest_path),
        }
    return result


def _verify_resources(path: Path) -> None:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != RESOURCE_FIELDS:
            raise RuntimeError(f"resource evidence has the wrong schema: {path}")
        rows = list(reader)
    if not rows or not all(row["elapsed_ms"] and row["phase"] for row in rows):
        raise RuntimeError(f"resource evidence is empty or incomplete: {path}")


def _verify_runtime_contract(report: dict[str, Any]) -> None:
    isolation = report["runtime_isolation"]
    if not (
        isolation["status"] == "verified"
        and isolation["future_frame_isolation"] is True
        and isolation["ground_truth_access"] is False
        and isolation["repository_access"] is False
        and isolation["media_access"] is False
        and isolation["network_mode"] == "none"
        and isolation["image_identity_verified"] is True
        and isolation["container_user_alignment_verified"] is True
        and isolation["mounts"] == [isolation["expected_mount"]]
        and isolation["expected_mount"] == {
            "destination": "/run/cvbench",
            "source": "<socket-only-runtime-dir>",
        }
        and isolation["requested"] == {
            "cpu_limit": 4,
            "memory_limit_mb": 2048,
            "network_access": False,
        }
        and isolation["applied"] == {"cpu_limit": 4.0, "memory_limit_mb": 2048.0}
    ):
        raise RuntimeError("committed report does not prove the required Docker isolation")

    resources = report["resources"]
    if not (
        resources["authoritative"] is True
        and resources["accounting_scope"] == "container_cgroup_v2_external"
        and resources["sample_count"] > 0
        and all(resources["accounting_availability"].values())
        and resources["cpu_time_seconds"] is not None
        and resources["peak_ram_bytes"] is not None
    ):
        raise RuntimeError("committed report does not prove authoritative external accounting")

    timing = report["timing"]
    provenance = report["provenance"]
    if not (
        timing["contract_version"] == "cvbench.timing-compute/v1"
        and timing["source"]["immutable"] is True
        and timing["replay"]["profile"] == "native"
        and timing["replay"]["rate"] == 1.0
        and timing["replay"]["allowlisted"] is True
        and timing["replay"]["native_real_time"] is True
        and timing["delivery"]["policy_version"] == "cvbench.delivery-lossless/v1"
        and report["leaderboard"]["policy_version"] == "cvbench.pareto/v1"
        and report["leaderboard"]["replay_class"] == "native"
        and provenance["raw_evidence_available"] is False
        and provenance["timing_compute_contract"] == "cvbench.timing-compute/v1"
        and provenance["delivery_policy"] == "cvbench.delivery-lossless/v1"
        and provenance["replay_profile"] == "native"
        and provenance["replay_rate"] == 1.0
    ):
        raise RuntimeError("committed report timing, delivery, or provenance contract drifted")


def _verify_report(
    path: Path,
    *,
    benchmark: dict[str, str],
    expected_scenarios: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    _assert_safe(path)
    report = json.loads(path.read_text())
    if report["benchmark"] != benchmark or report["outcome"]["status"] != "completed":
        raise RuntimeError(f"committed report has the wrong completed benchmark identity: {path}")
    scenarios = report["provenance"]["comparison_inputs"]["scenarios"]
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    if len(by_id) != len(scenarios) or by_id != expected_scenarios:
        raise RuntimeError(f"committed report scenario fingerprints differ from the repository: {path}")
    _verify_runtime_contract(report)
    return report


def _verify_license_and_catalog(repo_root: Path, isolated_report: Path) -> None:
    ingest = json.loads((repo_root / "scenarios/motchallenge-v1/ingest-manifest.json").read_text())
    audit = json.loads((repo_root / "scenarios/motchallenge-v1/visual-audit.json").read_text())
    if not (
        ingest["official_archives_only"] is True
        and ingest["public_detections_used"] is False
        and ingest["mots_or_mot15_representations_used"] is False
        and ingest["selected_sequence_ids"] == [value.upper() for value in MOT_IDS]
        and ingest["license"]["id"] == "CC-BY-NC-SA-3.0"
        and ingest["license"]["legalcode_bytes"] == 22_306
        and ingest["license"]["legalcode_sha256"] == LICENSE_LEGALCODE_SHA256
        and ingest["license_boundary"]["noncommercial_only"] is True
        and ingest["license_boundary"]["share_alike_required"] is True
        and "Original container PTS is unavailable and is not claimed" in ingest["cadence_disclosure"]
        and audit["manifest_sha256"] == ingest["manifest_sha256"]
    ):
        raise RuntimeError("MOT license, provenance, or cadence boundary drifted")
    if set(ingest["archive_audits"]) != set(OFFICIAL_ARCHIVES):
        raise RuntimeError("MOT archive inventory drifted")
    for name, expected in OFFICIAL_ARCHIVES.items():
        archive = ingest["archive_audits"][name]
        if archive["url"] != f"https://motchallenge.net/data/{name}" or any(
            archive[key] != value for key, value in expected.items()
        ):
            raise RuntimeError("MOT archive provenance is not official and pinned")

    metadata = yaml.safe_load((repo_root / "scenario-catalog/metadata.yaml").read_text())
    source = metadata["sources"]["motchallenge"]
    if (
        source["license"] != "CC-BY-NC-SA-3.0"
        or "noncommercial share-alike" not in source["license_boundary"]
        or source["archive_provenance"]
        != {
            name: {"bytes": value["bytes"], "sha256": value["sha256"]}
            for name, value in OFFICIAL_ARCHIVES.items()
        }
    ):
        raise RuntimeError("catalog MOT license boundary drifted")
    catalog_evidence = json.loads(
        (repo_root / "scenario-catalog/evidence/motchallenge-v1.json").read_text()
    )
    if not (
        catalog_evidence["schema_version"] == "cvbench.sanitized-baseline-evidence/v1"
        and catalog_evidence["validation_status"] == "completed"
        and catalog_evidence["report_sha256"] == _digest(isolated_report)
        and set(catalog_evidence["scenarios"]) == set(MOT_IDS)
    ):
        raise RuntimeError("catalog baseline evidence is not bound to the committed native-Linux report")


def verify(repo_root: Path, real_video_manifest: Path) -> None:
    repo_root = repo_root.resolve()
    evidence_manifest = repo_root / "evidence/motchallenge-v1/artifacts.sha256"
    verify_hash_bound_files(repo_root, evidence_manifest, EVIDENCE_FILES)
    mot_hashes = verify_corpus_manifests(repo_root, real_video_manifest.resolve())

    isolated_path = (
        repo_root
        / "evidence/motchallenge-v1/isolated/20260724T073004Z-68fc08ee/report.json"
    )
    combined_path = (
        repo_root
        / "evidence/motchallenge-v1/combined/20260724T075153Z-d36caca2/report.json"
    )
    isolated_fingerprints = _scenario_fingerprints(
        repo_root,
        "benchmarks/motchallenge-v1.yaml",
        mot_hashes,
    )
    combined_fingerprints = _scenario_fingerprints(
        repo_root,
        "benchmarks/public-whole-system-v3.yaml",
        mot_hashes,
    )
    if list(isolated_fingerprints) != MOT_IDS or set(combined_fingerprints) != COMBINED_IDS:
        raise RuntimeError("benchmark scenario inventory drifted")
    _verify_report(
        isolated_path,
        benchmark={"id": "motchallenge-known-public-corpus", "version": "1.0.0"},
        expected_scenarios=isolated_fingerprints,
    )
    _verify_report(
        combined_path,
        benchmark={"id": "public-whole-system-tracking", "version": "3.0.0"},
        expected_scenarios=combined_fingerprints,
    )
    _verify_resources(
        repo_root
        / "evidence/motchallenge-v1/isolated/20260724T073004Z-68fc08ee/resources.csv"
    )
    _verify_resources(
        repo_root
        / "evidence/motchallenge-v1/combined/20260724T075153Z-d36caca2/resources.csv"
    )
    _verify_license_and_catalog(repo_root, isolated_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--real-video-manifest",
        type=Path,
        default=Path("data/real-video-v2/artifacts.sha256"),
    )
    args = parser.parse_args()
    verify(args.repo_root, args.real_video_manifest)
    print("verified committed MOT/combined native-Linux evidence and both corpus manifests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
