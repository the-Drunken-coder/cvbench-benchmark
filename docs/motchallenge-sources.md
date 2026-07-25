# MOTChallenge dense pedestrian tranche

CVBench uses exactly ten known-public pedestrian sequences:

- MOT17-02, MOT17-04, MOT17-09, MOT17-10, MOT17-11, and MOT17-13 use the updated MOT17 ground truth from `MOT17Labels.zip` over one canonical copy of the original MOT16 JPEG pixels.
- MOT20-01, MOT20-02, MOT20-03, and MOT20-05 use the JPEGs and ground truth in `MOT20.zip`.

No public detections, MOTS/MOT15 duplicates, detector-specific pixel copies, vehicles, sparse clips, or split clips are used. The selected set contains 13,410 frames and 511.54 seconds at publisher-declared cadence. Strict class-1 truth contains 2,628 pedestrian trajectories and 1,239,994 annotated rows: 1,239,991 on-screen scored boxes plus three fully offscreen rows retained without boxes. Evaluator-only neutral rows contain 344 upstream identities and 293,614 boxes. The publisher headline envelope of 2,745 trajectories and about 1,442,300 boxes is reproduced only by including the MOT20 neutral rows; those rows are not described as scored pedestrian truth.

## License boundary

MOTChallenge assets and CVBench media/annotation derivatives are used and displayed only for this noncommercial hobby benchmark under [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/). Attribution and share-alike apply to those assets and derivatives. Repository code remains under the repository `LICENSE`; the asset license does not relicense the code.

## Pinned official inputs

The publisher exposes no visible archive checksum or version tag. CVBench therefore content-addresses the exact official bytes accepted at ingest and fails closed if they drift:

| Archive | Official URL | Retrieval UTC | Bytes | SHA-256 |
| --- | --- | --- | ---: | --- |
| MOT16.zip | `https://motchallenge.net/data/MOT16.zip` | 2026-07-24T01:45:04Z | 1,954,509,127 | `b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b` |
| MOT17Labels.zip | `https://motchallenge.net/data/MOT17Labels.zip` | 2026-07-24T01:45:04Z | 10,107,022 | `0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9` |
| MOT20.zip | `https://motchallenge.net/data/MOT20.zip` | 2026-07-24T01:45:07Z | 5,028,926,248 | `ebcf0e3d44e4f50b5357d24817e5db485d777633d1b8ca9e8380d1c8437dbdd7` |

The accepted CC BY-NC-SA 3.0 legalcode text is 22,306 bytes with SHA-256 `8812f83442fd0eca14eb0208988e190fdcbfebec58fa5459d3218edfdfdc5a32`.

`scenarios/motchallenge-v1/ingest-manifest.json` records the full ZIP inventory, CRC result, selected-member hashes, and path audit. Ingest rejects size/hash drift, CRC failure, duplicate or case-colliding members, absolute or parent paths, links and special files, missing/noncontiguous frames, JPEG dimension or cadence mismatch, duplicate frame/ID rows, ID/class drift, invalid visibility, and out-of-frame output boxes.

## Normalization and cadence

MOT one-based `xywh` becomes CVBench pixel-edge `xyxy`. Boundary-crossing rows are clipped only to the visible frame intersection; fully offscreen rows remain machine-auditable with `on_screen=false` and no box. All 26,416 changed rows are in `corrections.jsonl`. There are no manual annotation edits and no interpolated truth.

Timestamps are derived exactly from ordered JPEG ordinals and the publisher-declared fixed FPS:

```text
timestamp = (one_based_frame - 1) / publisher_fps
```

CVBench stores the nearest rational integer nanosecond. Original container presentation timestamps are unavailable and are not claimed. The public H.264 viewer derivative uses the same declared 25/30 FPS cadence; it is not evidence of original container timestamps.

## Scoring and runtime boundary

Marked class 1 maps to `person` with stable track IDs and exhaustive full-frame scoring. After target matching, official distractor, static-person, reflection, person-on-vehicle, and other non-target rows may neutralize a prediction. These rows and regions are evaluator-only.

Submitted containers receive progressive current-frame JPEG bytes and derived timestamps only. They never receive ground truth, boxes, identities, classes, ignore regions, ROI, detections, annotations, labels, future frames, or scoring hints. Results are known-public-corpus CVBench evaluation, not unseen generalization and not official MOTChallenge leaderboard scoring.

## Aggregation and correlation boundary

CVBench publishes both detection-weighted MOT metrics and an unweighted macro-average across the ten selected sequences, so MOT20-05 cannot dominate the summary merely because it is longest. The six MOT17-label/MOT16-pixel sequences and four MOT20 sequences are two correlated public-corpus source families, not ten statistically independent datasets. The pinned archives do not declare stable location-group identifiers, so CVBench does not invent location labels or claim location-level independence.

## Reproduction

Place the three untouched archives in `.local-ingest/motchallenge/`, then run:

```bash
python scripts/prepare_motchallenge.py --write-repository-files
python scripts/prepare_motchallenge.py --verify
python scripts/audit_motchallenge.py --review-status manual_review_completed
```

For a trusted native-Linux replay of the isolated MOT benchmark and the complete
26-scenario public suite, use a clean evidence directory:

```bash
scripts/run_trusted_mot_evidence.sh trusted-mot-evidence
```

That operator command requires the three already-provisioned official archives,
verifies their pinned bytes, hydrates both corpora, runs the real Docker-isolated
scoring lifecycle, sanitizes the reports, and hashes the resulting evidence. It
does not download archives or update committed evidence.

Ordinary pull-request CI is intentionally hermetic. It performs a fresh Docker
lifecycle and scored synthetic/MEVA runs from committed inputs, then
mandatorily verifies the committed native-Linux MOT and combined reports against
their exact report schemas, isolation/accounting contracts, provenance and
license boundaries, catalog/frame/ground-truth hashes, and both corpus artifact
manifest fingerprints. CI never treats an unavailable official host as success;
network ingest is outside that unprovisioned clean-checkout job.

Exploded media and normalized runtime data remain ignored under `data/motchallenge-v1/`. Committed evidence includes the pinned ingest manifest, exact per-frame hashes, normalized-GT hashes, machine-auditable corrections, public compressed annotations, deterministic viewer derivatives, and the 60-frame/12-track-per-sequence visual audit.
