# KITTI Tracking external training corpus

CVBench can normalize the official KITTI 2D multi-object tracking **training** set for local model training. This is an external training corpus only. Its images, labels, normalized output, and ground truth must never be committed, published through the scenario catalog, or used as CVBench public benchmark truth.

## Official files

Log in to the [official KITTI Tracking page](https://www.cvlibs.net/datasets/kitti/eval_tracking.php) and download exactly:

- `data_tracking_image_2.zip` — “left color images of tracking data set”; the importer reads only `training/image_02/0000` through `training/image_02/0020`.
- `data_tracking_label_2.zip` — “training labels of tracking data set”; the importer reads only `training/label_02/0000.txt` through `training/label_02/0020.txt`.

Do not use a mirror. No right-camera images, test labels, calibration, detections, development kit, raw data, or test-set truth are required. KITTI requires registered downloads and says that challenge training data may be used to learn algorithm parameters; test data must not be used to train or tune systems. See the [official download/submission policy](https://www.cvlibs.net/datasets/kitti/user_login.php).

Place the untouched archives in the ignored local directory:

```text
.local-ingest/kitti-tracking/
├── data_tracking_image_2.zip
└── data_tracking_label_2.zip
```

The repository ignores `.local-ingest/`. Do not commit either archive or any imported output.

## License and use boundary

KITTI states that its datasets are published under [Creative Commons Attribution-NonCommercial-ShareAlike 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) and are available for academic use only. The importer requires explicit `--accept-license`, records that notice and the official source pages, and marks every generated manifest `training_only` and `public_benchmark_truth: false`. Users remain responsible for attribution, noncommercial use, share-alike obligations, privacy, and the current KITTI terms.

## Import

With the project installed:

```bash
cvbench-import-kitti-tracking \
  --input .local-ingest/kitti-tracking \
  --output .local-ingest/kitti-tracking-cvbench \
  --accept-license
```

For a subset, repeat `--sequence`, for example `--sequence 0000 --sequence 0001`. Only training IDs `0000` through `0020` are accepted. Optional `--expected-images-sha256` and `--expected-labels-sha256` pins refuse unexpected archive bytes.

The importer writes one `sequences/NNNN/scenario.yaml` frame manifest plus `ground_truth.jsonl` and original PNG frames. `ingest-manifest.json` records:

- source archive and selected-member byte counts and SHA-256 hashes;
- every generated sequence-file hash (the ingest manifest excludes itself) and a deterministic aggregate content hash;
- the importer and mapping-policy versions;
- official provenance and license/use notice;
- exact sequence, frame, scored-label, and ignore-label counts.

The importer validates both ZIP inventories and the CRC of every selected member as it is read, rejects unsafe or ambiguous paths, and requires contiguous zero-based training frames, exact KITTI label rows, known classes/occlusion codes, stable track classes, unique per-frame track IDs, finite numbers, and in-frame ordered boxes. Output is assembled in a temporary directory and renamed only after full validation, so a refused import leaves no partial corpus.

## Mapping policy

All output timestamps preserve KITTI’s 10 Hz camera cadence exactly: `source_timestamp_ns = frame_index * 100,000,000`. Original track IDs and 2D boxes are retained. `truncation_fraction` preserves KITTI’s numeric truncation value; `truncated` is true when that fraction is greater than zero.

KITTI provides ordinal occlusion, not a numeric visible fraction. Therefore `visibility_fraction` is explicitly `null`, while source occlusion is retained as both its original code/label and the closest CVBench state:

| KITTI occlusion | CVBench occlusion |
| --- | --- |
| -1, `DontCare` not applicable | `unknown` plus preserved source sentinel |
| 0, fully visible | `none` |
| 1, partly occluded | `partial` |
| 2, largely occluded | `partial` plus source code/label |
| 3, unknown | `unknown` |

Class handling is explicit and versioned:

| KITTI class | CVBench class | Training state |
| --- | --- | --- |
| `Car` | `car` | score/train |
| `Pedestrian` | `person` | score/train |
| `Van` | `car` | ignore, neighboring class |
| `Person_sitting` | `person` | ignore, neighboring class |
| `Truck`, `Tram` | `car` | ignore, unsupported class |
| `Cyclist` | `person` | ignore, unsupported class |
| `Misc`, `DontCare` | `__ignore__` | class-agnostic ignore region |

Unknown classes fail the import. Nothing is silently converted to background.

Official `DontCare` rows use `-1` sentinels for truncation and occlusion. Those sentinels are accepted only for `DontCare`, retained in `source_truncation` and `source_occlusion`, and normalized to `truncation_fraction: null` and `occlusion: unknown`.
