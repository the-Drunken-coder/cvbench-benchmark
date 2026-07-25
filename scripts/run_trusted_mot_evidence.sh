#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

output_root=${1:-trusted-mot-evidence}
case "$output_root" in
  /*|*..*)
    echo "output directory must be a repository-relative path without '..': $output_root" >&2
    exit 2
    ;;
esac
if [[ -e "$output_root" ]]; then
  echo "refusing to overwrite existing evidence directory: $output_root" >&2
  exit 2
fi

for archive in MOT16.zip MOT17Labels.zip MOT20.zip; do
  if [[ ! -f ".local-ingest/motchallenge/$archive" ]]; then
    echo "missing pinned official archive: .local-ingest/motchallenge/$archive" >&2
    exit 2
  fi
done

python -m pip install -r requirements-real-video.lock
python -m pip install -r requirements-motchallenge.lock
docker build -f examples/Dockerfile.good -t cvbench-example-good:v1 .
docker build --platform linux/amd64 -f examples/Dockerfile.real-video-prep \
  -t cvbench-real-video-prep:v2 .
CVBENCH_REAL_VIDEO_PREP_IMAGE=cvbench-real-video-prep:v2 \
  scripts/prepare_real_video_container.sh --output data/real-video-v2

# This command reads only the three local, hash-pinned official archives. It has
# no download flag and fails closed on missing or changed bytes.
python scripts/prepare_motchallenge.py
python scripts/prepare_motchallenge.py --verify

cvbench run \
  --benchmark benchmarks/motchallenge-v1.yaml \
  --system systems/empty-floor-docker.yaml \
  --output "$output_root/motchallenge-runs"
python scripts/assert_docker_report.py "$output_root/motchallenge-runs" --motchallenge
python scripts/sanitize_ci_report.py \
  "$output_root/motchallenge-runs" \
  "$output_root/motchallenge-safe-runs"
python scripts/verify_ci_evidence.py \
  "$output_root/motchallenge-safe-runs" \
  data/motchallenge-v1/artifacts.sha256

cvbench run \
  --benchmark benchmarks/public-whole-system-v3.yaml \
  --system systems/empty-floor-docker.yaml \
  --output "$output_root/combined-runs"
python scripts/assert_docker_report.py "$output_root/combined-runs" --combined
python scripts/sanitize_ci_report.py \
  "$output_root/combined-runs" \
  "$output_root/combined-safe-runs"
python scripts/verify_ci_evidence.py \
  "$output_root/combined-safe-runs" \
  data/real-video-v2/artifacts.sha256 \
  data/motchallenge-v1/artifacts.sha256

python scripts/evidence_hashes.py "$output_root/artifacts.sha256" \
  "$output_root"/motchallenge-safe-runs/*/report.json \
  "$output_root"/motchallenge-safe-runs/*/resources.csv \
  "$output_root"/combined-safe-runs/*/report.json \
  "$output_root"/combined-safe-runs/*/resources.csv \
  data/real-video-v2/artifacts.sha256 \
  data/motchallenge-v1/artifacts.sha256
python scripts/evidence_hashes.py "$output_root/artifacts.sha256" --verify

echo "trusted MOT and combined evidence verified at $output_root"
