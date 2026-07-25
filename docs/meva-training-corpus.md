# Local MEVA KF1 training corpus

CVBench can prepare user-supplied public MEVA KF1 video and official
**training-level** annotations as a local model-training corpus. The result uses
the current `cvbench.scenario/v1` frame manifest and
`cvbench.ground-truth/v1` JSONL record shapes, but it is not benchmark truth:

- every manifest has `data_role: model_training_only` and
  `evaluation_eligible: false`;
- CVBench's scenario loader rejects the manifest if it is placed in a benchmark;
- only frames containing a positive MEVA geometry row are emitted;
- the annotation policy is `activity_bounded_positive_only`;
- a missing row means **unknown / not annotated**, never background, a negative
  example, or evidence that no other object is present.

This boundary matters because ActEV activities have explicit begin and end
rules. Object geometry is attached to those bounded activities, not to an
exhaustive inventory of every visible person, vehicle, or object in every
frame. Track IDs are therefore preserved exactly as upstream activity-object
track IDs; the importer does not merge them into inferred physical identities.

## Official source and license boundary

The [official MEVA site](https://mevadata.org/) identifies KF1, links its public
video and annotation sources, and distinguishes the 120.2-hour training-level
annotation effort from evaluation-level annotations. It warns that the
training-level set used a reduced audit step and may be less accurate.

The [official KF1 README](https://mevadata.org/resources/README-meva-kf1-data.html)
documents the public `mevadata-public-01` S3 source, current `r13` ground-camera
layout, filenames, and 30 FPS transcoded videos. The
[official transcoding FAQ](https://mevadata.org/transcoding-faq.html) states
that Kitware's training annotations and transcoded video start from the same
frame extraction.

The [official MEVA license text](https://mevadata.org/resources/MEVA-data-license.txt)
licenses the MEVA dataset by Kitware Inc. and IARPA under Creative Commons
Attribution 4.0 International. The importer requires a local byte-exact copy
with SHA-256
`bdeedfb765049c87f92a2450369ad70882fca3371190b2a6b7e560e103c922e8`.
If the official file changes, preparation fails closed until the change is
reviewed and the pinned hash is intentionally updated. CC BY 4.0 requires
attribution, a license notice/link, and an indication of modifications when
shared; the generated manifest and provenance retain those fields. This is a
technical summary, not legal advice.

The importer accepts annotation files only when all of the following are true:

- they are tracked and unmodified at a full Git commit;
- their checkout's `origin` is the official
  `gitlab.kitware.com/meva/meva-data-repo` repository, not a mirror;
- they are below
  `annotation/DIVA-phase-2/MEVA/kitware-meva-training/`;
- the `.geom.yml`, `.types.yml`, and `.r13.avi` stems match exactly.

## Exact local prerequisites

1. Python 3.11+ with this project and development dependencies installed:

   ```sh
   python -m pip install -e '.[dev]'
   ```

2. An ignored local ingest layout. `.local-ingest/` is excluded by Git:

   ```text
   .local-ingest/meva/
   ├── MEVA-data-license.txt
   ├── annotations/
   │   └── meva-data-repo/   # clean checkout from official Kitware GitLab
   ├── videos/
   │   └── <MEVA sequence stem>.r13.avi
   └── prepared/             # created by the importer
   ```

3. A user-supplied official 30 FPS KF1 `r13` ground-camera video from the public
   MEVA S3 source. The importer never downloads media.

4. A clean checkout of the official MEVA data repository containing the
   matching training-level `.geom.yml` and `.types.yml` files. Pin the checkout
   to the commit you intend to use. The importer never fetches annotations.

5. The official license text saved locally:

   ```sh
   curl --fail --location \
     https://mevadata.org/resources/MEVA-data-license.txt \
     --output .local-ingest/meva/MEVA-data-license.txt
   ```

Do not use `drop-4-hadcv22` for this path. The official KF1 README gives that
drop a separate challenge-specific restriction.

## Prepare one sequence

All path arguments are below `.local-ingest/meva`. Example:

```sh
python scripts/prepare_meva_training.py \
  --scenario-id meva-kf1-training-g340-20180305-131500 \
  --video videos/2018-03-05.13-15-00.13-20-00.bus.G340.r13.avi \
  --geom annotations/meva-data-repo/annotation/DIVA-phase-2/MEVA/kitware-meva-training/2018-03-05/13/2018-03-05.13-15-00.13-20-00.bus.G340.geom.yml \
  --types annotations/meva-data-repo/annotation/DIVA-phase-2/MEVA/kitware-meva-training/2018-03-05/13/2018-03-05.13-15-00.13-20-00.bus.G340.types.yml
```

The output is
`.local-ingest/meva/prepared/meva-kf1-training-g340-20180305-131500/`:

- `scenario.yaml`: positive-frame manifest and explicit training-only policy;
- `ground_truth.jsonl`: source IDs, classes, boxes, frame indices, rational
  source-relative timestamps, and unknown visibility;
- `frames/`: JPEGs only for source frames with at least one positive geometry;
- `provenance.json`: input byte sizes/hashes, annotation origin/commit/paths,
  license attribution/hash, tool version, and normalized output hashes;
- `artifacts.sha256`: exact coverage and hashes for every prepared artifact.

The importer rejects an existing output directory rather than overwriting it.
Running the same inputs to two empty output directories produces byte-identical
artifacts with the same installed OpenCV version.

Verify an existing output without touching source inputs:

```sh
python scripts/prepare_meva_training.py \
  --scenario-id meva-kf1-training-g340-20180305-131500 \
  --verify-only
```

## Training use

Read `scenario.yaml` and join JSONL rows to frames by
`source_timestamp_ns` (or the preserved `source_frame_index`). Each row is a
positive supervised example. Do not synthesize negative boxes, background-only
frames, entry/exit events, visibility fractions, or physical-object identity
continuity from missing MEVA rows. If a trainer requires exhaustive negatives,
this corpus is incompatible with that training policy unless a separate,
audited exhaustive annotation layer is supplied.

Prepared media and annotations remain under the ignored ingest directory. Do
not commit them to this repository or add these manifests to `benchmarks/`.
