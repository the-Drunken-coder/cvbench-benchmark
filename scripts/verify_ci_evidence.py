#!/usr/bin/env python3
"""Verify that the small Docker evidence packet contains no restricted payloads."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from cvbench.reporting import validate_redacted_report

HASH_LINE = re.compile(r"^[0-9a-f]{64}  [^\n]+$")
FORBIDDEN_FRAGMENTS = (
    '"bbox_xyxy"',
    '"source_frame_index"',
    '"system-output.jsonl"',
    '"ground-truth.jsonl"',
    '"raw_output"',
    '"api_key"',
    '"password"',
    '"secret"',
)


def _assert_safe(path: Path) -> None:
    text = path.read_text()
    lowered = text.lower()
    assert not any(fragment in lowered for fragment in FORBIDDEN_FRAGMENTS), path
    if path.suffix == ".json":
        report = json.loads(text)
        validate_redacted_report(report)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_ci_evidence.py RUNS ARTIFACT_MANIFEST")
    runs = Path(sys.argv[1])
    reports = sorted(runs.glob("*/report.json"))
    resources = sorted(runs.glob("*/resources.csv"))
    assert len(reports) == 1, reports
    assert len(resources) == 1, resources
    for path in [*reports, *resources]:
        _assert_safe(path)
    manifest = Path(sys.argv[2])
    lines = [line for line in manifest.read_text().splitlines() if line.strip()]
    assert lines, manifest
    for line in lines:
        assert HASH_LINE.fullmatch(line), line
        relative_path = line.split("  ", 1)[1]
        assert not Path(relative_path).is_absolute()
        assert ".." not in Path(relative_path).parts
    print(f"verified safe evidence: {reports[0]}, {resources[0]}, {manifest}")


if __name__ == "__main__":
    main()
