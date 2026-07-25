# Validated MOTChallenge expansion research

Status: approved conditional tranche; no unconditional datasets qualified.

The research gate applied the project’s dense, continuous, public, track-identity, annotation, cadence, licensing, and noncommercial-display criteria without weakening them. Exactly ten pedestrian sequences qualified conditionally:

`MOT17-02`, `MOT17-04`, `MOT17-09`, `MOT17-10`, `MOT17-11`, `MOT17-13`, `MOT20-01`, `MOT20-02`, `MOT20-03`, and `MOT20-05`.

The six MOT17 selections use updated MOT17 ground truth over canonical MOT16 pixels. The four MOT20 selections use their native archive representation. Detector-specific MOT17 labels were proven byte-identical, so one canonical pixel copy is used. Public detector files and duplicate MOTS/MOT15 representations are excluded.

The exact derived set contains 13,410 JPEG frames and 511.54 seconds at publisher-declared 25/30 FPS. Strict class-1 truth contains 2,628 trajectories and 1,239,994 annotated rows: 1,239,991 on-screen scored boxes plus three fully offscreen rows retained without boxes. Evaluator-only neutral rows contain 344 identities and 293,614 boxes. The publisher-declared 2,745-trajectory/about-1,442,300-box envelope is reproducible only when the MOT20 neutral rows are included; it is not the scored class-1 count.

Primary publisher endpoints:

- MOTChallenge: <https://motchallenge.net/>
- MOT16 archive: <https://motchallenge.net/data/MOT16.zip>
- MOT17 label archive: <https://motchallenge.net/data/MOT17Labels.zip>
- MOT20 archive: <https://motchallenge.net/data/MOT20.zip>
- CC BY-NC-SA 3.0 legal code: <https://creativecommons.org/licenses/by-nc-sa/3.0/legalcode.txt>

The publisher provides no visible archive checksums or release tags. CVBench pins the independently hashed bytes and fails closed on drift. Original video-container PTS is unavailable. All CVBench timestamps and public derivative cadence are derived from ordered JPEG ordinals and publisher-declared fixed FPS; neither this report nor the implementation claims original timestamps.

Scores retain detection-weighted MOT metrics and add an unweighted ten-sequence macro-average. MOT17-label/MOT16-pixel and MOT20 are documented as two correlated public-corpus source families; the archives do not supply stable location-group identifiers, so no location independence is inferred.

No rejected corpus, vehicle claim, keyframe/sparse work, scenario split, or broader dataset expansion is part of this result.
