# Third-party model notice

The learned examples use official YOLOX ONNX checkpoints:

| Model | Upstream file | SHA-256 |
|---|---|---|
| YOLOX-Nano | `YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx` | `c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d` |
| YOLOX-Tiny | `YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx` | `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7` |
| YOLOX-L | `YOLOX/releases/download/0.1.1rc0/yolox_l.onnx` | `7860ae79de6c89a3c1eb72ae9a2756c0ccfbe04b7791bb5880afabd97855a411` |

YOLOX is copyright Megvii, Inc. and its affiliates and is distributed under
the Apache License 2.0. The Dockerfiles download the official release assets
directly and verify these hashes. Model binaries are not committed to this
repository.

The local association implementations are original compact reference code.
ByteTrack-style and observation-centric terminology describes the algorithms'
design influences; no ByteTrack or OC-SORT source is vendored.
