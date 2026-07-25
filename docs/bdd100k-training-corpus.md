# BDD100K MOT 2020 training corpus

CVBench can prepare user-supplied BDD100K MOT 2020 train and validation data for model training. This is an external training corpus, not CVBench benchmark or evaluation data. The importer never downloads BDD100K, never uses a mirror, and never adds BDD100K media or annotations to Git.

## Official files to supply

Sign in to the [official Berkeley BDD100K download portal](https://bdd-data.berkeley.edu/download.html), accept its terms, and download exactly these two portal artifacts:

- **MOT 2020 Images**
- **MOT 2020 Labels**

Do not substitute **Videos**, **100K Images**, **Detection 2020 Labels**, **MOTS 2020 Images**, or any third-party mirror. Extract the two official artifacts together under the ignored local source directory:

```text
.local-ingest/bdd100k/source/bdd100k/
├── images/
│   └── track/
│       ├── train/
│       │   └── <video-name>/<video-name>-<frame-index>.jpg
│       └── val/
│           └── <video-name>/<video-name>-<frame-index>.jpg
└── labels/
    └── box_track_20/
        ├── train/
        │   └── <video-name>.json
        └── val/
            └── <video-name>.json
```

The required layout is the one documented by the [official BDD100K model repository](https://github.com/SysCV/bdd100k-models/blob/main/doc/PREPARE_DATASET.md). Test labels are intentionally unsupported: official public test truth is not supplied, and this path is training-only.

## Prepare

From the repository root:

```bash
python scripts/prepare_bdd100k_mot.py
```

The default output is `.local-ingest/bdd100k/prepared/`. Both source and output are covered by `.gitignore`. Use `--split train` or `--split val` to prepare one split. Use `--source-root` and `--output` only when a different local layout is needed.

The output directory must not already exist. The importer prepares in a temporary sibling and publishes it only after every requested sequence passes validation, so a rejection leaves no partial corpus.

Each sequence contains:

- `scenario.yaml`: the CVBench frame manifest, referencing the original local JPEG bytes;
- `ground_truth.jsonl`: normalized CVBench ground-truth rows;
- `corpus-manifest.json` at the corpus root: source and output inventory, sizes, SHA-256 hashes, importer and mapping policy versions, totals, provenance, timing policy, and the license notice.

## Timing policy

The [BDD100K MOT release format](https://github.com/JonathonLuiten/TrackEval/blob/master/docs/BDD100k-format.txt) specifies 5 FPS annotated sequences. Those labels do not provide truth for every frame of the original 30 FPS videos.

- When every Scalabel frame has a `timestamp`, the importer preserves the exact timestamp deltas in milliseconds and records each native timestamp.
- Otherwise it uses the annotated `frameIndex` (or legacy `index`) at exactly 5 Hz.
- Missing annotated indices remain timestamp gaps. The importer never creates intermediate frames or boxes and never treats unannotated 30 FPS frames as ground truth.

The manifest's `frame_index` remains the source annotated index; it is not renumbered.

## Mapping policy

Policy version `cvbench.bdd100k-mot-person-vehicle/v1` maps the release format's 11 MOT categories as follows:

| BDD100K MOT category | CVBench class | Training row |
| --- | --- | --- |
| `pedestrian`, `rider` | `person` | eligible |
| `car`, `bus`, `truck`, `train`, `motorcycle`, `bicycle` | `vehicle` | eligible |
| `other person` | `bdd100k-excluded/other-person` | explicit ignore |
| `trailer` | `bdd100k-excluded/trailer` | explicit ignore |
| `other vehicle` | `bdd100k-excluded/other-vehicle` | explicit ignore |

Rows with BDD100K's `Crowd` attribute are also explicit ignores. Excluded, crowd, or unsupported labels are never silently dropped into background: each known excluded label produces a boxed row with its source category, track ID, and an `ignore` reason represented by its fields; an unknown category rejects the entire import.

Track IDs are namespaced by video and preserved in `source_track_id`. Source-pixel `box2d`, binary occlusion, truncation, and crowd flags are retained. Binary `occluded=true` maps to categorical `partial`, while exact `visibility_fraction` remains `null`; BDD100K does not provide a numeric visible fraction.

## Fail-closed checks

Preparation rejects:

- missing, extra, empty, undecodable, non-JPEG, or symlinked inputs;
- a train/validation mismatch between label JSON files and image directories;
- a mismatch between a sequence's labeled image names and its exact JPEG inventory;
- malformed or mixed Scalabel frame indices/timestamps;
- duplicate frames or duplicate track IDs within a frame;
- a track that changes category;
- missing or non-boolean occluded/truncated attributes;
- unknown categories, invalid boxes, non-finite coordinates, or boxes outside source dimensions;
- an existing output directory.

Every source JSON/JPEG and generated scenario/ground-truth file is inventoried with its byte length and SHA-256 digest. Repeating the same import with the same importer produces byte-identical output.

## License boundary

The [official BDD100K download page](https://bdd-data.berkeley.edu/download.html) grants use, copying, modification, and distribution for educational, research, and not-for-profit purposes without a signed agreement. Commercial rights under that notice are granted only to BDD and BAIR Commons members and affiliates; other commercial users are directed to UC Berkeley's Office of Technology Licensing.

Confirm that the intended training use fits those terms before importing. The prepared manifest repeats this notice and the official source URL, but it is not legal advice and does not grant additional rights. Keep the original images, labels, and prepared corpus local unless your license permits distribution.
