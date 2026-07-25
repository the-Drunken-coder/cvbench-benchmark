# Public scenario catalog

`/scenarios/` publishes every scenario referenced by the current benchmark manifests: the exact union is derived at build time and must contain 16 stable IDs (13 synthetic and 3 real video). There is no hidden-current tier. Exact benchmark JPEGs, complete public annotations, fixed scoring boundaries, provenance, licenses, transformation notices, content hashes, and allowlisted first-party baseline summaries are directly inspectable.

All current data can be tuned to or memorized. Private run ordering and runtime isolation prevent future-frame and host-data access while a system is executing; they do not turn recognizable public data into a secret. A future hidden-challenge design is outside catalog Version 1.

## Stable endpoints

- `/.well-known/cvbench-scenarios.json` discovers the current catalog and its SHA-256.
- `/scenario-catalog/v1/catalog.json` is the revalidated catalog index.
- `/scenario-catalog/v1/scenarios/{stable-id}.json` is a stable scenario detail document.
- `/scenario-catalog/v1/{frame,annotation,baseline}-manifests/sha256/{sha256}.json` contains immutable content-addressed manifests.
- `/scenario-catalog/v1/assets/sha256/{sha256}.jpg` contains an immutable exact benchmark frame.

Indexes and stable detail documents revalidate. Content-addressed media and manifests use a one-year immutable browser cache policy.
Unknown catalog JSON and JPEG paths return honest `404` responses with machine-appropriate MIME types and no immutable cache. Human navigation keeps `/scenarios/` as its stable entry point and uses the site's explicit `404.html` for unknown pages.

## Deterministic allowlisted build

From `control-plane/`:

```bash
npm ci
```

The `postinstall` build writes only to `control-plane/dist`. It derives mechanical fields from the current benchmark manifests and their referenced scenario manifests. `scenario-catalog/metadata.yaml` is deliberately limited to titles, descriptions, failure modes, source attribution, and transformation notes. `scenario-catalog/baselines.json` binds each scenario to one of three committed, sanitized evidence manifests. The builder hashes those sources, reconciles every derived summary, and publishes only their allowlisted fields; raw run reports are not copied into the site.

The build rejects a benchmark/catalog set mismatch, non-public status, unknown or private-looking source fields, path traversal, a symlink in any allowlisted path component, undeclared source files or extensions, malformed or out-of-frame geometry, annotation classes outside a declared scenario ontology, non-contiguous frames, sequence/timestamp mismatch, JPEG dimension mismatch, hash mismatch, and private-artifact filename patterns. It completes this preflight before replacing output, and accepts only `control-plane/dist` or a direct `dist-test-*` directory. Three committed, size-limited frame archives must expand to exactly the 450 names and bytes in `scenarios/real-video-v2/expected-frame-sha256.txt`; synthetic media is read from the benchmark scenario directories. Identical frame bytes share one content-addressed asset.

Limits enforced by the build are:

- each asset below 25 MiB;
- each JPEG at or below the recommended 2 MiB;
- catalog JSON at or below 256 KiB;
- each scenario JSON at or below 512 KiB;
- complete initial catalog output at or below 50 MiB.

Two clean builds must have the same sorted relative paths and identical bytes. `build-evidence.json` records every generated path, size, and SHA-256, and tests independently recompute them.

## Public/private boundary

The catalog publishes scenario manifests, exact media, complete annotations, scoring semantics, first-party baseline summaries, and validation hashes. It never publishes operator credentials, adjudication notes, submission secrets, D1 internals or exports, contact values, raw untrusted system output, failure packets, private logs, or deployment credentials.

The viewer creates all untrusted text with `textContent` and numeric SVG attributes; it has no `innerHTML`, arbitrary URL proxy, remote script, remote media, R2, or encoded-video dependency. The CSP limits scripts, images, media, and connections to the same origin and sets `object-src 'none'`.

## Scoring disclosure

Current scoring is class-aware. Synthetic annotations are exhaustive, and `synthetic-false-detection` deliberately has no targets. The three real-video-v2 scenarios exhaustively annotate every supported visible moving person, vehicle, or dog across the complete image at the native 30 FPS cadence. They have no ROI or ignore rows: misses, duplicate predictions, false tracks, and background predictions anywhere in the frame are penalized, while HOTA and IDF1 measure detection and temporal identity association. Synthetic scenarios retain scenario-specific target and ignore constructs where those deterministic tests require them; target matching precedes ignore matching, `ignore_region` requires at least 50% prediction-area coverage, and ordinary ignore matching requires IoU greater than or equal to 0.5.

Public overlays are inspection aids only. The runner sends a submitted system progressive current-frame JPEG bytes and timestamps; it never sends boxes, track identities, ignore masks, annotations, future frames, or object-selection hints.

## Exact-frame playback

The viewer fetches the unchanged content-addressed JPEGs, verifies every SHA-256, decodes a bounded active-scenario window, and presents verified pixels on a persistent canvas. The canvas is never cleared while another frame is loading, so buffering keeps the last exact frame and its synchronized overlay visible instead of flashing a blank surface.

Playback is driven by `requestAnimationFrame` and monotonic browser time mapped onto the published source timestamps. Rendering or decoding delay does not extend the source clock: when presentation falls behind, the viewer advances to the newest decoded frame due at that source time. Pause, scrub, previous/next, Home/End, and keyboard stepping load any individual benchmark frame exactly.

Normal connections keep at most 48 MiB of decoded active-scenario frames, use no more than four concurrent fetch/decode operations, and look ahead no more than 750 ms or the memory limit. Paused poster loading fetches the current frame and at most two neighbors. Save-Data fetches only the poster while paused and limits active playback lookahead to 150 ms. Scenario changes abort the old media controller, close decoded bitmaps, discard queued work, and retain the existing generation check for manifest navigation races. No viewer transcode or additional catalog media is produced.

The reproducible before/after browser trace is recorded in `docs/scenario-playback-evidence.json`; `npm run measure:playback` repeats the implementation measurement.

## Cloudflare Static Assets basis

The design uses the existing Worker Static Assets binding and generated `dist` directory. Cloudflare's current limits allow 20,000 files per Worker version on Free and 100,000 on Paid, with a 25 MiB per-file limit. Static assets default to `Cache-Control: public, max-age=0, must-revalidate` plus an ETag; `_headers` may assign immutable caching to fingerprinted assets. Sources checked 2026-07-22:

- <https://developers.cloudflare.com/workers/platform/limits/#static-assets>
- <https://developers.cloudflare.com/workers/static-assets/binding/>
- <https://developers.cloudflare.com/workers/static-assets/headers/>

The catalog is comfortably within those limits and therefore does not need R2 or encoded video.
