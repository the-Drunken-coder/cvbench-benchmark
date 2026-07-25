# MOTChallenge implementation prompt

This file preserves the exact active implementation direction supplied for the ten-sequence tranche.

> The three required untouched archives are now present under the repository's `.local-ingest/motchallenge/` directory: MOT16.zip, MOT17Labels.zip, and MOT20.zip. Resume the existing ten-clip implementation now. First independently hash, inventory, path-safety audit, and validate these exact bytes; record retrieval/provenance/license evidence and fail closed on any mismatch. Then complete only the approved ten dense pedestrian sequences, validation, deterministic hydration, viewer/catalog/queue/scoring integration, leakage/isolation checks, manual overlay/track audit, and required evidence. Preserve existing scenarios and PR #7/#8 behavior. Open one ready public PR only after all implementation evidence passes. Do not merge, deploy, invoke review bots, create reviewers, or begin any other expansion. Once the ready PR and final evidence report are preserved, stop development for user testing and report completion.

Controlling decisions carried forward from the approved tranche:

- CVBench is entirely noncommercial/hobby. MOTChallenge assets and derivatives use CC BY-NC-SA 3.0 with attribution, share-alike, and a separate code/asset license boundary.
- The only sequences are MOT17-02, -04, -09, -10, -11, -13 and MOT20-01, -02, -03, -05. Sparse/keyframe, vehicle, rejected-corpus, split-clip, and broader dataset work is out of scope.
- Publisher-declared ordinal-JPEG 25/30 FPS is the accepted derived cadence. Original container PTS is unavailable and must never be implied.
- Scored truth is marked class-1 pedestrian. Official non-target rows are evaluator-only neutral ignore after target matching and are never sent to a submitted container.
- HOTA, IDF1, identity switches, fragmentation, misses, and false-track evidence are required. Results are known-public-corpus CVBench evaluation, not unseen generalization or official MOTChallenge scoring.
- The existing synthetic scenarios, API compatibility, timing/accounting contracts, progressive no-future-frame delivery, Docker isolation, least privilege, no network, and resource limits remain intact.
- One ready public PR is the terminal development action. No merge, deployment, reviewers, bots, or follow-on fixes are authorized without a new explicit request.
