#!/usr/bin/env python3
"""Write or verify hashes for the exact sanitized CI evidence packet."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        if args.files:
            raise SystemExit("files are not accepted with --verify")
        lines = args.manifest.read_text().splitlines()
        if not lines:
            raise RuntimeError("evidence hash manifest is empty")
        for line in lines:
            expected, relative = line.split("  ", 1)
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or digest(path) != expected:
                raise RuntimeError(f"evidence hash mismatch: {relative}")
        return 0
    if not args.files:
        raise SystemExit("at least one evidence file is required")
    paths = sorted({path for path in args.files}, key=lambda item: item.as_posix())
    if any(not path.is_file() or path.is_absolute() or ".." in path.parts for path in paths):
        raise RuntimeError("evidence paths must be relative regular files")
    args.manifest.write_text("".join(f"{digest(path)}  {path.as_posix()}\n" for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
