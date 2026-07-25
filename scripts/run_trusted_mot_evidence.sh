#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

output_root=$(
  python3 -I scripts/trusted_mot_environment.py \
    "${1:-trusted-mot-evidence}"
)

for archive in MOT16.zip MOT17Labels.zip MOT20.zip; do
  if [[ ! -f ".local-ingest/motchallenge/$archive" ]]; then
    echo "missing pinned official archive: .local-ingest/motchallenge/$archive" >&2
    exit 2
  fi
done

for image in cvbench-example-good:v1 cvbench-real-video-prep:v2; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "missing pre-provisioned trusted evidence image: $image" >&2
    exit 2
  fi
done

mkdir -- "$output_root"
export PYTHONPATH="$repo_root/src"
export PYTHONSAFEPATH=1

CVBENCH_REAL_VIDEO_PREP_IMAGE=cvbench-real-video-prep:v2 \
  scripts/prepare_real_video_container.sh --output data/real-video-v2

# This command reads only the three local, hash-pinned official archives. It has
# no download flag and fails closed on missing or changed bytes.
python3 scripts/prepare_motchallenge.py
python3 scripts/prepare_motchallenge.py --verify

python3 -m cvbench.cli run \
  --benchmark benchmarks/motchallenge-v1.yaml \
  --system systems/empty-floor-docker.yaml \
  --output "$output_root/motchallenge-runs"
python3 scripts/assert_docker_report.py "$output_root/motchallenge-runs" --motchallenge
python3 scripts/sanitize_ci_report.py \
  "$output_root/motchallenge-runs" \
  "$output_root/motchallenge-safe-runs"
python3 scripts/verify_ci_evidence.py \
  "$output_root/motchallenge-safe-runs" \
  data/motchallenge-v1/artifacts.sha256

python3 -m cvbench.cli run \
  --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/empty-floor-docker.yaml \
  --output "$output_root/combined-runs"
python3 scripts/assert_docker_report.py "$output_root/combined-runs" --combined
python3 scripts/sanitize_ci_report.py \
  "$output_root/combined-runs" \
  "$output_root/combined-safe-runs"
python3 scripts/verify_ci_evidence.py \
  "$output_root/combined-safe-runs" \
  data/real-video-v2/artifacts.sha256 \
  data/motchallenge-v1/artifacts.sha256

python3 scripts/evidence_hashes.py "$output_root/artifacts.sha256" \
  "$output_root"/motchallenge-safe-runs/*/report.json \
  "$output_root"/motchallenge-safe-runs/*/resources.csv \
  "$output_root"/combined-safe-runs/*/report.json \
  "$output_root"/combined-safe-runs/*/resources.csv \
  data/real-video-v2/artifacts.sha256 \
  data/motchallenge-v1/artifacts.sha256
python3 scripts/evidence_hashes.py "$output_root/artifacts.sha256" --verify

echo "trusted MOT and combined evidence verified at $output_root"
