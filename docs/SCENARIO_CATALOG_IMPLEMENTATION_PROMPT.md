# Scenario catalog implementation input (verbatim)

The block below preserves the exact implementation input for the public scenario catalog change. Terminology inside this historical input is intentionally unchanged.

> You are the lead implementation task for CVBench PR #5, the public scenario catalog and viewer. Start from current origin/main, which now includes merged PR #4 at e8df36ebec225839bed7e8fa7aefec7617b8ee53. The parent task is orchestration-only; you own research confirmation, implementation, validation, branch/commit/push, and opening a focused draft PR #5. Do not invoke Greptile, CodeRabbit, or Codex review; do not resolve review threads; do not merge; do not deploy production.
>
> Product contract:
> - Every currently supported scenario is public and directly inspectable on the public website: all 13 synthetic plus 3 real-video scenarios, with no hidden-current tier.
> - Build /scenarios/ with list/filter/detail views, exact frame-sequence playback, play/pause, scrubber, previous/next, keyboard controls, selected-frame inspection, and safe overlay toggles for targets, ignores, scoreable ROI, faults, and labels.
> - Publish stable machine-readable endpoints: /.well-known/cvbench-scenarios.json, /scenario-catalog/v1/catalog.json, and /scenario-catalog/v1/scenarios/{stable-id}.json plus content-addressed frame/annotation/baseline manifests.
> - Show scenario/pack version and status, task/failure modes, frames/timing/FPS/resolution, target/class/annotation policy, scoring boundaries, provenance, source, author, license/attribution, transformation details, preparation/container identity, hashes, baseline system summary, and validation evidence.
> - State clearly that all current scenarios are public and can be tuned/memorized; runtime isolation does not make public data secret. Future hidden challenges are out of scope.
> - Keep operator credentials, adjudication notes, submission secrets, D1 internals, raw untrusted outputs, failure packets, private logs, and deployment credentials private.
> - Publish allowlisted first-party baseline summaries only; do not copy unfiltered run reports into the site.
> - All current media/annotations must be inspectable. Use deterministic exact JPEG benchmark frame sequences and same-origin SVG/DOM overlays. No arbitrary URL proxy, SSRF, third-party scripts, R2, or encoded-video dependency unless verified impossible under current Static Assets limits.
> - Respect Commons licensing: synthetic and rv1-a7f3 are CC0; rv1-b2c8 and rv1-c3d1 are CC BY 3.0 with creator/title/source/license/modification notice.
> - Derive mechanical metadata from current benchmark/scenario manifests; maintain a small curated source only for titles/descriptions/failure modes/attribution/transformation notes. Build-time allowlist only; reject hidden/private statuses, symlinks, traversal, undeclared files/extensions, malformed geometry, hash mismatches, and any secret/private artifact pattern.
> - Use Static Assets through the existing Worker. Prefer generated control-plane/dist with build copying only declared assets; verify official current Cloudflare limits/behavior before finalizing. Content-addressed assets immutable-cache; indexes revalidate.
> - Deterministic preview/catalog generation: two clean builds byte-identical; every published hash verified; media/build byte budgets enforced.
> - Accessibility/mobile/bandwidth/failure UX: keyboard controls and labels, visible focus, reduced motion, screen-reader announcements, 320–430px no overflow, 44px controls, poster-first/current+next-two prefetch, Save-Data disables prefetch, honest missing/corrupt-media state.
>
> Terminology correction is required across current user-facing surfaces:
> - Hero: “Benchmark the whole vision system.”
> - CTA: “Submit a system image.”
> - Use system, system under test (SUT), or submitted system image. Reserve model for an actual learned model component.
> - Explain that one immutable linux/amd64 OCI image is a packaging/reproducibility/security boundary, not a one-model or one-process assumption. Explicitly support detector + tracker + temporal memory + association + filtering + post-processing pipelines, including multi-process systems, provided they connect to the progressive socket, emit CVBENCH_READY, and speak cvbench.track/v1.
> - Describe one linux/amd64 image, network disabled, 4 CPUs/2048 MB, one progressive socket-directory mount, no extra mounts/Docker socket as the current execution envelope.
> - Preserve /api/v1 wire/storage compatibility for model_version, response model, internal modelVersion, and D1 model_version. Present them as system version/submitted system and document them as compatibility names. No schema rename or v2 redesign in this PR.
> - Update control-plane/public/index.html, app.js, control-plane/src/app.js OpenAPI/contract prose, tests, docs/control-plane.md, and surrounding lifecycle descriptions. Preserve verbatim historical prompt/spec documents unchanged when required, but add current corrected terminology.
> - Add a case-insensitive user-facing terminology regression rejecting “the system is the model,” “submit a model,” “submit your model,” “model container(s),” and “model-submission interface,” excluding only literal v1 compatibility fields/storage and verbatim historical docs.
>
> Scoring disclosure:
> - Current benchmarks class-aware.
> - Synthetic annotations exhaustive; synthetic-false-detection deliberately has no targets.
> - Real annotations targeted/non-exhaustive.
> - Real predictions wholly outside fixed ROI are out of scope.
> - Target matching precedes ignore matching.
> - ignore_region uses &gt;=50% prediction-area coverage; ordinary ignore uses IoU &gt;=0.5.
> - Background hallucinations inside ROI and duplicate target predictions remain penalized.
> - Never imply every real-video object is annotated.
>
> Acceptance gates:
> - Catalog union exactly equals every scenario referenced by current benchmark manifests (expected 16, but derive and assert).
> - Every scenario has exact playable media, full public annotations, stable JSON, and per-scenario baseline evidence including track_id_churn.
> - Two clean builds byte-identical; media/manifests/annotations hashes match.
> - Both CC BY scenarios have complete attribution/modification notices in UI and JSON.
> - Asset &lt;25 MiB; recommended frame &lt;=2 MiB; catalog &lt;=256 KiB; scenario JSON &lt;=512 KiB; initial catalog &lt;=50 MiB.
> - CSP/XSS: textContent/numeric DOM attributes, no innerHTML, no remote media/scripts, object-src 'none', media-src 'self'.
> - Leakage checks fail on credentials, .dev.vars, D1 exports, contacts, notes, raw reports, failure packets, undeclared files.
> - Existing control-plane/D1/submission API security and tests remain intact.
> - Visual QA screenshots for desktop/mobile synthetic, real ROI/ignore, empty-ground-truth, and long-sequence cases.
> - Proportionate full Python/control-plane tests, catalog tests, deterministic double-build, Docker scored baseline/E2E, and local Worker smoke where practical.
>
> Keep the solution simple and elegant. Preserve the full original project implementation spec and exact relevant implementation prompt in the PR description or repository documentation as required by project practice. Open a draft PR only after the branch is pushed. Return the PR URL, exact head, file/architecture summary, complete scenario inventory, media/license/hash evidence, baseline evidence, screenshots, tests, and blockers.
