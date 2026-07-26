from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_learned_example_catalog_points_to_runnable_assets() -> None:
    catalog = (ROOT / "examples/models/README.md").read_text()
    expected = {
        "lite-mot": (
            "examples/Dockerfile.lite-mot",
            "systems/example-lite-mot-docker.yaml",
            "cvbench.examples.lite_mot",
        ),
        "balanced-mot": (
            "examples/Dockerfile.balanced-mot",
            "systems/example-balanced-mot-docker.yaml",
            "cvbench.examples.balanced_mot",
        ),
    }
    for name, (dockerfile, manifest, module) in expected.items():
        assert f"{name}/README.md" in catalog
        assert dockerfile in catalog
        assert manifest in catalog
        assert (ROOT / dockerfile).is_file()
        system = yaml.safe_load((ROOT / manifest).read_text())
        assert system["runtime"]["command"] == ["python", "-m", module]
        assert system["resources"]["network_access"] is False


def test_model_downloads_are_hash_pinned_and_weights_are_not_committed() -> None:
    downloads = {
        "examples/Dockerfile.lite-mot": (
            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
            "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
        ),
        "examples/Dockerfile.balanced-mot": (
            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx",
            "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
        ),
    }
    dockerfiles = [(ROOT / path).read_text() for path in downloads]
    assert all("ADD --checksum=sha256:" in item for item in dockerfiles)
    assert all("chmod 0444 /app/models/" in item for item in dockerfiles)
    assert all("python:3.12-slim@sha256:" in item for item in dockerfiles)
    assert all("opencv-python-headless==4.13.0.92" in item for item in dockerfiles)
    for path, (url, digest) in downloads.items():
        dockerfile = (ROOT / path).read_text()
        assert url in dockerfile
        assert f"ADD --checksum=sha256:{digest}" in dockerfile
    assert not any(
        path.suffix in {".onnx", ".pt", ".pth", ".engine", ".tflite"}
        for path in (ROOT / "examples").rglob("*")
        if path.is_file()
    )
