from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from scripts.trusted_mot_environment import (
    locked_versions,
    validate_output_root,
    verify_locked_versions,
)


def test_output_root_is_canonical_and_contained(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "evidence").mkdir()

    output = validate_output_root(repo_root, "evidence/new-run")

    assert output == repo_root.resolve() / "evidence/new-run"


@pytest.mark.parametrize("requested", ["/tmp/evidence", "../outside", "nested/../../outside"])
def test_output_root_rejects_absolute_and_parent_paths(tmp_path: Path, requested: str) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(RuntimeError, match="repository-relative"):
        validate_output_root(repo_root, requested)


@pytest.mark.parametrize("dangling", [False, True])
def test_output_root_rejects_existing_output_symlink(tmp_path: Path, dangling: bool) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = tmp_path / "outside"
    if not dangling:
        target.mkdir()
    (repo_root / "evidence").symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="must not contain symlinks"):
        validate_output_root(repo_root, "evidence")


def test_output_root_rejects_symlinked_existing_parent(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo_root / "parent").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="must not contain symlinks"):
        validate_output_root(repo_root, "parent/new-run")


def test_output_root_rejects_existing_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    output = repo_root / "evidence"
    output.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        validate_output_root(repo_root, "evidence")


def test_locked_versions_require_exact_direct_pins(tmp_path: Path) -> None:
    first = tmp_path / "first.lock"
    second = tmp_path / "second.lock"
    first.write_text("Example_Package==1.2.3\n")
    second.write_text("# separate corpus dependency\nOther.Package==4.5.6\n")

    assert locked_versions([first, second]) == {
        "example-package": ("Example_Package", "1.2.3"),
        "other-package": ("Other.Package", "4.5.6"),
    }


@pytest.mark.parametrize(
    "line",
    [
        "--index-url https://packages.example.invalid/simple",
        "package>=1.0",
        "package==1.0; python_version >= '3.11'",
        "https://packages.example.invalid/package.whl",
    ],
)
def test_locked_versions_reject_index_resolution_and_non_exact_inputs(
    tmp_path: Path, line: str
) -> None:
    lock_file = tmp_path / "requirements.lock"
    lock_file.write_text(f"{line}\n")

    with pytest.raises(RuntimeError, match="exact NAME==VERSION"):
        locked_versions([lock_file])


def test_dependency_version_mismatch_fails_closed(tmp_path: Path) -> None:
    lock_file = tmp_path / "requirements.lock"
    lock_file.write_text("example-package==1.2.3\n")

    with pytest.raises(RuntimeError, match="expected 1.2.3, found 9.9.9"):
        verify_locked_versions([lock_file], installed_version=lambda _name: "9.9.9")


def test_missing_locked_dependency_fails_closed(tmp_path: Path) -> None:
    lock_file = tmp_path / "requirements.lock"
    lock_file.write_text("example-package==1.2.3\n")

    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    with pytest.raises(RuntimeError, match="missing locked dependency"):
        verify_locked_versions([lock_file], installed_version=missing)
