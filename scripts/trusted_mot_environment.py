#!/usr/bin/env python3
"""Fail-closed environment checks for the trusted MOT evidence runner."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

LOCK_FILES = ("requirements-real-video.lock", "requirements-motchallenge.lock")
EXACT_REQUIREMENT = re.compile(r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^;\s]+)")


def _canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock_files: Iterable[Path]) -> dict[str, tuple[str, str]]:
    """Read only exact direct dependency pins from the committed lock files."""
    required: dict[str, tuple[str, str]] = {}
    for lock_file in lock_files:
        for line_number, raw_line in enumerate(lock_file.read_text().splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = EXACT_REQUIREMENT.fullmatch(line)
            if match is None:
                raise RuntimeError(
                    f"{lock_file}:{line_number} must contain one exact NAME==VERSION pin"
                )
            name = match.group("name")
            version = match.group("version")
            canonical_name = _canonical_distribution_name(name)
            previous = required.get(canonical_name)
            if previous is not None and previous[1] != version:
                raise RuntimeError(
                    f"conflicting locked versions for {name}: {previous[1]} and {version}"
                )
            required[canonical_name] = (name, version)
    if not required:
        raise RuntimeError("trusted evidence dependency locks are empty")
    return required


def verify_locked_versions(
    lock_files: Iterable[Path],
    installed_version: Callable[[str], str] = importlib.metadata.version,
) -> dict[str, str]:
    """Require the active interpreter to match every direct dependency pin."""
    verified: dict[str, str] = {}
    for canonical_name, (name, expected) in sorted(locked_versions(lock_files).items()):
        try:
            actual = installed_version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"missing locked dependency: {name}=={expected}") from exc
        if actual != expected:
            raise RuntimeError(
                f"locked dependency mismatch for {name}: expected {expected}, found {actual}"
            )
        verified[canonical_name] = actual
    return verified


def validate_output_root(repo_root: Path, requested_value: str) -> Path:
    """Return a missing, canonical, symlink-free path contained by the repository."""
    if not requested_value:
        raise RuntimeError("output directory must not be empty")
    requested = Path(requested_value)
    if requested.is_absolute() or ".." in requested.parts:
        raise RuntimeError(
            f"output directory must be a repository-relative path without '..': {requested_value}"
        )

    canonical_repo = repo_root.resolve(strict=True)
    candidate = canonical_repo.joinpath(requested)
    current = canonical_repo
    meaningful_parts = [part for part in requested.parts if part not in {"", "."}]
    for index, part in enumerate(meaningful_parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"output directory must not contain symlinks: {requested_value}")
        if index < len(meaningful_parts) - 1 and not stat.S_ISDIR(mode):
            raise RuntimeError(
                f"output directory parent is not a directory: {current.relative_to(canonical_repo)}"
            )

    canonical_candidate = candidate.resolve(strict=False)
    try:
        canonical_candidate.relative_to(canonical_repo)
    except ValueError as exc:
        raise RuntimeError(
            f"output directory escapes repository root: {requested_value}"
        ) from exc

    if os.path.lexists(candidate):
        raise RuntimeError(f"refusing to overwrite existing evidence directory: {requested_value}")
    return canonical_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_root")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    try:
        output_root = validate_output_root(repo_root, args.output_root)
        lock_files = [repo_root / relative for relative in LOCK_FILES]
        verified = verify_locked_versions(lock_files)
    except RuntimeError as exc:
        parser.error(str(exc))
    versions = ", ".join(f"{name}=={version}" for name, version in verified.items())
    print(f"verified pre-provisioned locked dependencies: {versions}", file=sys.stderr)
    print(output_root.relative_to(repo_root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
