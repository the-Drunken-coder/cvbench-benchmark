# Recovered clean-video training corpus

The five clean Pixabay/Pexels originals recovered under
`Downloads/Recovered Computer Vision Videos/Originals` are local, training-only inputs. They are not part of the public
CVBench benchmark and they are not evaluation ground truth.

`cvbench-import-recovered-videos` accepts only the five exact SHA-256-pinned source files. It preserves the original video
bytes, samples native video at 5 FPS into 1280-pixel training images, and writes YOLO-format labels plus confidence-bearing
JSONL proposals from the pinned YOLOX-X COCO model. The accepted ontology is limited to the people and dog actually present.
One source that produced two historical overlay videos is imported only once. The complete output stays under ignored
`.local-ingest/` storage.

The generated manifest deliberately says:

- `data_role: model_training_only`
- `evaluation_eligible: false`
- `annotation_scope: machine_generated_non_exhaustive_object_detections`
- `unknown_is_background: false`

Those fields matter. A missing model proposal is unknown, not a verified negative. The generated YOLO files are convenient
training inputs, but they remain pseudo-labels even after the visual audit. Zero-proposal frames remain in the corpus for
review but are omitted from `train.txt` and `validation.txt`, so they are not silently taught as verified background. The
complete contact-sheet pass removed distant dune scenery misclassified as vehicles and a static tree root repeatedly
misclassified as a second person; those corrections are part of the deterministic importer and manifest. The scenario loader
also rejects training-only manifests if somebody tries to route one into benchmark scoring.

## Local build

The current local model runtime is the immutable `cvbench-example-advanced-mot` image. Run its exact image ID and model
bytes without network access:

```bash
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD:/workspace" \
  --volume "$HOME/Downloads/Recovered Computer Vision Videos/Originals:/source:ro" \
  --workdir /workspace \
  --env PYTHONPATH=/workspace/src \
  --entrypoint python \
  sha256:bc04a103d4c2e1698d4818c39a84b1bb229a4c0243ea8aae806b9bcdd624c5ac \
  -m cvbench.recovered_training \
  --source-dir /source \
  --output-dir /workspace/.local-ingest/recovered-videos-v1 \
  --model /app/models/yolox_x.onnx \
  --runtime-id sha256:bc04a103d4c2e1698d4818c39a84b1bb229a4c0243ea8aae806b9bcdd624c5ac
```

The command refuses an existing output directory rather than overwriting training assets. Verify the completed corpus with:

```bash
cvbench-import-recovered-videos \
  --output-dir .local-ingest/recovered-videos-v1 \
  --verify-only
```

`corpus.yaml` records source, model, runtime, sampling, class-count, and license provenance. `inventory.sha256` covers every
generated asset. `review/` contains sequential 5-by-5 contact sheets whose ledger covers every sampled frame exactly once.

## Public preview publication

The Cloudflare site publishes five compact annotated previews at `/training/`; it does not re-host the clean stock files or
the full local training package. `scripts/build_recovered_training_previews.py` renders the 5 FPS sampled images at 960x540
with burned-in boxes, confidence, source time, and a training-only warning. The output manifest pins both source and preview
hashes and records attribution and license links.

The control-plane build accepts only those five transformed H.264 assets, checks every byte count and SHA-256, enforces the
25 MiB Cloudflare Static Assets per-file limit, and publishes content-addressed media under
`/training-media/v1/assets/sha256/`. The separate discovery document is
`/.well-known/cvbench-training-media.json`. None of these routes are referenced by benchmark manifests or scoring.

This transformation matters for provenance and licensing: the previews present CVBench's annotation work and training
disclosure rather than redistributing the clean Pixabay/Pexels source files as standalone stock footage. Every card links to
the creator, source page, and current license page.
