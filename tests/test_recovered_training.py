from pathlib import Path

import pytest

from cvbench.recovered_training import (
    MODEL_SHA256,
    SOURCE_CLIPS,
    Detection,
    _excluded_by_visual_audit,
    sample_frame_indices,
    verify_corpus,
)


def test_source_inventory_is_five_unique_hash_pinned_videos() -> None:
    assert len(SOURCE_CLIPS) == 5
    assert len({clip.filename for clip in SOURCE_CLIPS}) == 5
    assert len({clip.sha256 for clip in SOURCE_CLIPS}) == 5
    assert all(len(clip.sha256) == 64 for clip in SOURCE_CLIPS)
    assert len(MODEL_SHA256) == 64


def test_sample_frame_indices_preserve_native_endpoints_without_duplicates() -> None:
    indices = sample_frame_indices(300, 30.0, 5.0)
    assert indices == list(range(0, 300, 6))
    assert sample_frame_indices(2, 30.0, 5.0) == [0]


@pytest.mark.parametrize("values", [(0, 30.0, 5.0), (30, 0.0, 5.0), (30, 30.0, 0.0)])
def test_sample_frame_indices_reject_invalid_metadata(values: tuple[int, float, float]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        sample_frame_indices(*values)


def test_verify_requires_a_training_only_manifest(tmp_path: Path) -> None:
    (tmp_path / "corpus.yaml").write_text(
        "schema_version: cvbench.training-corpus/v1\n"
        "data_role: benchmark_evaluation\n"
        "evaluation_eligible: true\n"
    )
    with pytest.raises(ValueError, match="training-only"):
        verify_corpus(tmp_path)


def test_visual_audit_excludes_only_the_known_static_tree_root() -> None:
    false_tree_root = Detection((900, 420, 960, 500), "person", 0.6)
    actual_walker = Detection((700, 380, 820, 640), "person", 0.9)
    assert _excluded_by_visual_audit("pixabay-212474-forest-walk", false_tree_root, 1280, 720)
    assert not _excluded_by_visual_audit("pixabay-212474-forest-walk", actual_walker, 1280, 720)
    assert not _excluded_by_visual_audit("pixabay-28855-ravine", false_tree_root, 1280, 720)
