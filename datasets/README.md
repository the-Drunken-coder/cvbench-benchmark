# Dataset consumption boundary

This repository runs and scores benchmarks. It does not create, reconcile,
review, or certify benchmark truth.

New datasets arrive only as immutable releases from the dataset repository.
Each benchmark upgrade pins one deterministic
`<dataset-id>-<version>.tar.gz` archive with
[`cvbench.dataset-lock/v1`](../schemas/dataset-lock-v1.schema.json), then installs
it below ignored `data/datasets/<dataset-id>/`:

```bash
python scripts/install_dataset_release.py datasets/locks/<dataset>.lock.json
```

The archive must have exactly one top-level `<dataset-id>-<version>/` directory
containing:

```text
dataset.yaml
release-manifest.json
clips/<clip-id>/video.mp4
clips/<clip-id>/tracks.jsonl
clips/<clip-id>/source.json
clips/<clip-id>/review.jsonl
schemas/...
licenses/...
```

`release-manifest.json` uses `cvbench.dataset-release/v1`. It identifies a
certified dataset and its certification timestamp, assigns a canonical role to
every file, binds every clip to its media/truth/provenance/review hashes, and
contains a canonical self-content hash. The benchmark installer accepts only
`evaluation_eligible: true`, `data_role: benchmark_truth`, and
`annotation_scope: exhaustive_visible`; training-only, candidate, sparse, and
activity-bounded packages cannot become scored truth. The installer checks that
manifest plus the outer archive hash, rejects links and unsafe paths, verifies
the exact inner file set, and installs only after all checks pass. A local
`--archive` is allowed for CI and air-gapped use but must match the same lock.

Dataset changes are intentionally two-stage:

1. The dataset repository authors, reviews, certifies, and publishes a release.
2. A benchmark PR updates a lock and explicit benchmark adapter after reviewing
   the release. It must not modify labels.

After installation, materialize only the explicitly selected clips into the
runner's frame-scenario format:

```bash
python scripts/materialize_dataset_release.py \
  datasets/locks/<dataset>.lock.json \
  --clip <clip-id>
```

Materialization verifies the installed release again, decodes the certified
video at its declared frame count and dimensions, converts annotation field
names without changing boxes/classes/timestamps, and writes an ignored
`data/materialized/<dataset-id>/<version>/` tree plus a hash manifest. The
selected scenario YAML paths can then be referenced by a versioned benchmark
manifest. The adapter never edits the installed release or makes annotation
decisions.

The checked-in `scenarios/real-video-v2` tree predates this split. It is retained
only as the frozen compatibility fixture required to reproduce historical v2
and current public-suite results. It is not a template for future datasets.
