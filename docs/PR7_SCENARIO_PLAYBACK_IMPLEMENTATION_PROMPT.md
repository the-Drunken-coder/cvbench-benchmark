# Scenario playback implementation prompt (verbatim)

```text
You are the implementation owner for a focused CVBench public scenario-viewer UX PR. The parent task is orchestration-only; do the implementation, validation, branch/commit/push, and open a ready PR. Start from current origin/main containing merged PR #6. Do not merge or deploy production.

User problem: public scenario videos visibly flash and advance slowly frame-by-frame instead of playing smoothly at native cadence. Current control-plane/public/scenario-app.js uses per-frame JPEG swapping with setTimeout and awaits showFrame, causing decode/network time to accumulate and blank/flash transitions.

Required outcome:
- Native 24/30/60 FPS sequences play visually smoothly at their exact source cadence; 30 FPS real-video scenarios must look like ordinary 30 FPS video.
- No white/blank flash, layout shift, image disappearance, or overlay desynchronization during playback, including cache-cold playback.
- Playback clock must be drift-corrected against monotonic time/source timestamps. If decoding falls behind, the viewer may skip presentation frames to catch up, but must not silently slow the source clock. Scrubbing/step controls must still expose every exact benchmark frame.
- Preserve exact benchmark JPEG bytes, annotations, hashes, provenance, source timestamps, frame inspection, safe overlays, keyboard controls, reduced-motion behavior, 320/390/430 mobile layout, Save-Data behavior, CSP, same-origin-only assets, and all public/private boundaries.
- Prefer the simplest robust design: bounded ahead-of-playhead fetch+decode, predecoded/double-buffered image or canvas presentation, requestAnimationFrame scheduling, and honest buffering state. Do not add a lossy viewer-only transcode unless exact-frame/overlay parity and asset budgets cannot be met and the tradeoff is explicitly justified.
- Do not preload the whole catalog. Bound memory/network per active scenario; cancel stale navigation requests; preserve poster-first loading and missing/corrupt-media UX.
- Add real-browser tests measuring source-clock cadence, bounded drift, no blank frames, cache-cold start, 0.5x/1x/2x, play/pause/resume/end, scrubbing during playback, navigation races, overlays, Save-Data, mobile, reduced motion, and accessibility announcements. Include before/after capture or trace evidence.
- Preserve the exact implementation prompt in the PR description/repo history per project practice.

Run full control-plane/catalog/browser tests, deterministic build/hash/budget checks, Worker preview smoke, and existing Python/security regressions proportionately. Return PR URL, exact head, architecture choice, measured before/after cadence/blank evidence, tests, and blockers. Do not invoke final Greptile/CodeRabbit/Codex reviews; parent control room owns review follow-through.
```
