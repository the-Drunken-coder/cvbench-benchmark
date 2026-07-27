# PR #6 implementation prompt (verbatim)

The following three inputs are preserved verbatim in the order received.

## Initial implementation input

```text
Start a new implementation tranche from current merged main, intended as PR #6. The user has rejected the current real-video benchmark design, especially `real-video-v1 / rv1-a7f3 / Pedestrian crowd crossing`, as sparse, slideshow-like, target-centric, and not representative of real tracking. This is a new branch/PR; do not amend or reopen merged PR #5. Do not deploy or merge.

Outcome: redesign the public real-video scenarios and scoring so CVBench evaluates realistic whole-system multi-object tracking across full-frame video, not designated-target following.

Hard product requirements:
- Full image is the scorable region for every current real-video scenario. Do not crop scoring to a hand-picked ROI. Do not use ordinary ignore regions as a substitute for exhaustive labeling.
- Submitted systems receive progressive frames/timestamps only. They must never receive target boxes, ignore masks, ground truth, future frames, or hints about which object to follow.
- Ground truth must exhaustively cover every visible supported moving object throughout the sequence with stable IDs: at minimum people and relevant road objects (cars/other vehicles), and add dogs/other supported moving-object classes when present. Define a small explicit ontology rather than vague labels; document occlusion/truncation/visibility semantics.
- Preserve the source video’s real cadence and timestamps. Public playback and runner input must be realistically continuous at the source/native cadence (at least 24 FPS for these scenarios; 24/30/60 are acceptable according to source). Do not expand a 2 FPS extraction by duplicating/interpolating frames. If current sources or licenses cannot support dense native-cadence ground truth, replace them with legally redistributable real MOT dataset clips that can.
- Prefer established, legally clear datasets with dense multi-object track annotations. Verify redistribution and derivative rights from primary license/source records. Record creator/dataset, source URL/version/split/sequence, license, modifications, native FPS, exact frame range, deterministic preparation command, checksums, and annotation provenance.
- AI assistance may bootstrap annotation review, but generated/interpolated labels cannot become benchmark truth without actual visual inspection and deterministic human-reviewed correction. Record the audit evidence without exposing private scorer truth to submitted containers.
- Metrics must reward temporal identity consistency and complete scene understanding: class-aware detection/association, HOTA and IDF1 (or rigorously justified equivalents), misses/false tracks, ID switches, fragmentation, track completeness, duplicate/background penalties, and latency/resource reporting. Remove target-first sparse-match logic from these full-frame scenarios.
- Update baseline runs at the new cadence and produce scored Docker E2E evidence. Preserve online/no-future-frame isolation, no network, digest pinning, resource limits, and no-ground-truth leakage.
- Redesign the public scenario viewer for normal 1x video playback at native FPS with pause, frame-accurate scrubbing, timestamps/frame number, playback speed controls if useful, and clear whole-scene annotation overlays. Do not present target/ignore/ROI controls as the main model contract. Explain that overlays are public human inspection aids and are never sent to systems.
- All supported scenarios remain public and directly inspectable. Synthetic scenarios may retain scenario-specific constructs if required, but the user-facing language must make clear that real-video scenarios are exhaustive full-frame tracking.
- Preserve public/operator privacy boundaries, prompt/spec preservation, secure allowlisted deterministic build, machine-readable catalog endpoints, accessibility/mobile requirements, and existing submission API compatibility unless an explicit versioned migration is necessary.

Begin with a live audit of all current real-video sources, frame rates, licenses, annotation density, runner/scorer assumptions, UI behavior, and likely compute/storage impact. Make simple, elegant choices. Then implement the complete tranche on a new `codex/` branch, validate with real visual evidence and Docker scored E2E, push, and open a ready PR that preserves this exact implementation prompt in full. The PR description must explicitly state which old behaviors were removed/replaced and quantify scenario frames, native FPS, durations, tracks/classes, storage, baseline scores, and runtime. Do not invoke review bots yet; report the PR URL/head and evidence for independent review.
```

## Public queue inclusion input

```text
Add one verified audit finding to the PR #6 scope: the clean-room YOLO acceptance task confirmed the current public control-plane runner queues only `benchmarks/persistent-target-tracking.yaml` (12 synthetic scenarios), not the separate real-video tranche. The dense full-frame redesign must not remain catalog-only. Update the versioned public benchmark/control-plane contract so a submitted whole system can actually be evaluated on the new dense real-video scenarios—either as a clearly versioned combined default suite or an explicit validated benchmark selection, choosing the simplest compatible design. Public docs/OpenAPI/catalog/results must state exactly which scenario set a submission runs. Preserve existing API compatibility where possible, make any migration explicit/versioned, and test the public queue -&gt; isolated Docker run -&gt; scored callback lifecycle on the dense suite. No deployment or merge.
```

## Replacement-boundary input

```text
The user has now made the replacement boundary explicit for PR #6: remove the entire old real-video evaluation corpus and replace it only with external datasets that already include tracking labels. Treat this as a hard requirement layered onto the existing dense full-frame work.

Required diff boundary:
- Delete/retire all three current Wikimedia Commons real-video sources (`rv1-a7f3`, `rv1-b2c8`, `rv1-c3d1`) and every associated prepared-frame/hash/corpus manifest, sparse target/ignore/ROI annotation, catalog metadata entry, source/provenance text, baseline mapping/evidence, screenshots/contact sheets, tests, and documentation that describes them. No old real-video media or sparse-label behavior should remain reachable or implied by the current benchmark/catalog.
- Replace them only with legally redistributable external dataset sequences whose upstream release already includes per-frame tracking ground truth: bounding geometry, class labels, and stable track identity. Do not create a new benchmark sequence from an unlabeled ordinary video.
- CVBench may normalize/convert annotations and may visually verified-correct upstream duplicate IDs, missing movers, or geometry errors, but every correction must be explicitly documented and machine-auditable against the pinned upstream annotation record. No unreviewed model interpolation becomes truth.
- Keep the full-frame, &gt;=24 FPS native-cadence, exhaustive class-aware, no-hints-to-SUT, HOTA/IDF1, public viewer, queue-selectability, Docker E2E, licensing, privacy, and no-ground-truth-leakage requirements already assigned.
- Use a clean versioned scenario/corpus identity for the replacement rather than pretending old IDs still refer to the same content. Remove stale old IDs from the current catalog/benchmark union while preserving historical prompt/spec files verbatim.
- Preserve deterministic synthetic scenarios: they are generated tests with exact native ground truth, not the unlabeled real-video corpus the user is rejecting. In the PR description, call the new corpus evaluation/benchmark data and explain that it is not model training data unless a participant separately chooses to train on the public calibration material.
- PR description must enumerate every old artifact removed and every new upstream-labeled dataset/sequence added, including upstream label density, native FPS, class/track/frame counts, corrections, license/redistribution basis, hashes, storage/runtime impact, and scored baselines.

Continue on the existing new branch/PR #6 work. Do not deploy, merge, or invoke review bots before reporting the ready PR.
```
