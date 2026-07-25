# External MOT17/MOT20 training corpus

CVBench can deterministically import the official MOTChallenge MOT17 and MOT20
training data into its current `cvbench.scenario/v1` frame manifests and
`cvbench.ground-truth/v1` JSONL. This is an external, local training corpus. It
is not part of any benchmark manifest, public scenario catalog, submission run,
viewer, evidence bundle, or repository artifact.

The import contains 11 unique videos: all seven MOT17 training videos and all
four MOT20 training videos. MOT17 publishes each base video under DPM, FRCNN,
and SDP detector variants. The importer requires their updated annotations and
sequence metadata to agree, emits one canonical copy of the original official
pixels, and excludes public detections. Variants therefore do not inflate the
sequence count.

## Exact local prerequisites

Place these untouched official archives in `.local-ingest/motchallenge/`.
CVBench does not download them, accept mirrors, or fall back to unpinned files.

| Required file | Exact official URL | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `MOT16.zip` | `https://motchallenge.net/data/MOT16.zip` | 1,954,509,127 | `b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b` |
| `MOT17Labels.zip` | `https://motchallenge.net/data/MOT17Labels.zip` | 10,107,022 | `0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9` |
| `MOT20.zip` | `https://motchallenge.net/data/MOT20.zip` | 5,028,926,248 | `ebcf0e3d44e4f50b5357d24817e5db485d777633d1b8ca9e8380d1c8437dbdd7` |

`MOT16.zip` supplies the canonical pixels reused by MOT17.
`MOT17Labels.zip` supplies the updated MOT17 annotations. This smaller official
pair avoids materializing three identical MOT17 pixel copies while retaining
the published MOT17 labels.

The archive directory and generated `data/motchallenge-training-v1/` tree are
ignored by Git. Do not commit, upload, or place either tree in a public scenario
or viewer path.

## Import and verify

From the repository root:

```bash
python3 scripts/import_motchallenge_training.py import
python3 scripts/import_motchallenge_training.py verify
```

Import refuses to replace an existing output directory. Remove or relocate an
old generated tree deliberately before regenerating it.

The importer rejects absent or changed archives, invalid ZIPs, failed CRCs,
unsafe or colliding member paths, links and special files, missing or extra
frames, JPEG dimension drift, detector-variant disagreement, cadence/geometry
drift, malformed annotations, duplicate frame/identity rows, identity/class
drift, unknown classes, invalid visibility, and invalid boxes.

Every sequence directory contains:

- `scenario.yaml`: ordered frames with zero-based frame indices and native
  published cadence;
- `frames/`: byte-identical official JPEGs, represented once per unique video;
- `frames.sha256`: every frame hash;
- `ground_truth.jsonl`: CVBench records with stable source identities; and
- hashes for the scenario and normalized annotations in the corpus manifest.

The corpus root contains `artifacts.sha256` and `corpus-manifest.json`. The
manifest records accepted archive hashes and inventory hashes, selected-member
hashes, importer/mapping policy versions, source URLs, sequence statistics,
class mapping, deduplication policy, license boundary, and output hashes.

## Mapping policy

Published fixed FPS and one-based frame ordinals become exact rational
scenario-relative timestamps:

```text
timestamp = (one_based_frame - 1) / published_fps
```

The nearest integer nanosecond is stored. Ordered JPEGs do not provide original
container presentation timestamps, so the importer does not claim them.

MOT one-based `xywh` numeric values are retained as
`source_mot.bbox_xywh` and converted to zero-based pixel-edge `xyxy`.
`source_mot.bbox_xyxy_unclipped` preserves the direct conversion. CVBench
`bbox_xyxy` is the visible-frame intersection; fully offscreen source geometry
remains under `source_mot`.

All official class IDs 1 through 13 retain distinct semantic `class_id` values.
Marked class 1 (`person`) is the official scored target. Other rows preserve
their classes and identities with `evaluation_state: ignore`; person-on-vehicle,
static-person, distractor, and reflection rows are identified as distractors,
while classes 9, 10, 11, and 13 retain explicit stable ignore-region IDs.
Official visibility is preserved as a numeric fraction, with derived `none`,
`partial`, or `full` occlusion.

## Attribution and license boundary

MOTChallenge MOT17/MOT20 data and generated annotation/media derivatives are
attributed to [MOTChallenge](https://motchallenge.net/) and remain under
[Creative Commons Attribution-NonCommercial-ShareAlike 3.0
Unported](https://creativecommons.org/licenses/by-nc-sa/3.0/). Use is
noncommercial; attribution and share-alike obligations apply to the dataset and
derivatives. The repository code remains under the repository `LICENSE`; the
dataset license does not relicense the code.
