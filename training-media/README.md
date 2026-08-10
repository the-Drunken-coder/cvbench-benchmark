# Public training-media previews

`recovered-videos-v1/` contains five compact, transformed H.264 previews generated from the ignored local recovered-video
training corpus. They are not the clean source files. Each preview is rendered at 960x540 and 5 FPS with burned-in
pseudo-label boxes, confidence, source time, and an explicit training-only disclosure.

`recovered-videos-v1/publication.json` pins every preview and original source by SHA-256 and records source attribution,
license, split, class counts, and training semantics. The control-plane build validates that manifest, rejects drift and
symlinks, then publishes content-addressed MP4s and a machine-readable catalog under `/training-media/v1/`.

Regenerate from the verified ignored corpus with:

```bash
python scripts/build_recovered_training_previews.py
```

The generator refuses an existing output directory. Use `--verify-only` to check the committed publication instead.
