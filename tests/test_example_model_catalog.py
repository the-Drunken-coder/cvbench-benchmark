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
        assert (ROOT / dockerfile).is_file()
        system = yaml.safe_load((ROOT / manifest).read_text())
        assert system["runtime"]["command"] == ["python", "-m", module]
        assert system["resources"]["network_access"] is False


def test_model_downloads_are_hash_pinned_and_weights_are_not_committed() -> None:
    hashes = {
        "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
        "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
    }
    dockerfiles = [
        (ROOT / "examples/Dockerfile.lite-mot").read_text(),
        (ROOT / "examples/Dockerfile.balanced-mot").read_text(),
    ]
    assert all("ADD --checksum=sha256:" in item for item in dockerfiles)
    assert all("chmod 0444 /app/models/" in item for item in dockerfiles)
    assert all("python:3.12-slim@sha256:" in item for item in dockerfiles)
    assert all("opencv-python-headless==4.13.0.92" in item for item in dockerfiles)
    assert all(any(digest in item for item in dockerfiles) for digest in hashes)
    assert not any(
        path.suffix in {".onnx", ".pt", ".pth", ".engine", ".tflite"}
        for path in (ROOT / "examples").rglob("*")
        if path.is_file()
    )
