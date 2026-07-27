#!/usr/bin/env python3
"""Create a separate public-safe copy of a Docker benchmark report."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path
from typing import Any

from cvbench.reporting import validate_redacted_report, validate_report

REDACTION_MARKER = {
    "schema_version": "cvbench.audit-redacted/v1",
    "redacted": True,
    "reason": "annotation and prediction geometry is restricted to the runner",
}
REDACTION = {
    "schema_version": "cvbench.redaction/v1",
    "redacted": True,
    "reason": "restricted audit geometry, diagnostics, and host paths removed for CI publication",
}


def _safe_mount(mount: dict[str, Any]) -> dict[str, Any]:
    safe = dict(mount)
    if "source" in safe:
        safe["source"] = "<socket-only-runtime-dir>"
    return safe


def sanitize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Remove restricted audit/raw diagnostics while retaining score evidence."""
    validate_report(report)
    safe = copy.deepcopy(report)
    safe["schema_version"] = "cvbench.report-redacted/v1"
    safe["source_schema_version"] = "cvbench.report/v1"
    safe["redaction"] = copy.deepcopy(REDACTION)
    safe["audit_evidence"] = copy.deepcopy(REDACTION_MARKER)

    isolation = safe.get("runtime_isolation")
    if isinstance(isolation, dict):
        if isinstance(isolation.get("expected_mount"), dict):
            isolation["expected_mount"] = _safe_mount(isolation["expected_mount"])
        if isinstance(isolation.get("mounts"), list):
            isolation["mounts"] = [
                _safe_mount(mount) if isinstance(mount, dict) else mount for mount in isolation["mounts"]
            ]

    safe["diagnostics"] = {
        "schema_version": "cvbench.diagnostics-redacted/v1",
        "redacted": True,
        "reason": "submitted-system stderr and collector diagnostics are restricted to the runner",
        "match_count": report.get("diagnostics", {}).get("match_count", 0)
        if isinstance(report.get("diagnostics"), dict)
        else 0,
    }
    outcome = safe.get("outcome")
    if isinstance(outcome, dict) and outcome.get("errors"):
        outcome["errors"] = ["<redacted diagnostic error>"]
    validate_redacted_report(safe)
    return safe


def sanitize_runs(source: Path, destination: Path) -> Path:
    reports = sorted(source.glob("*/report.json"))
    resources = sorted(source.glob("*/resources.csv"))
    if len(reports) != 1 or len(resources) != 1 or reports[0].parent.name != resources[0].parent.name:
        raise ValueError("expected exactly one run with report.json and resources.csv")
    run_destination = destination / reports[0].parent.name
    run_destination.mkdir(parents=True, exist_ok=True)
    report = json.loads(reports[0].read_text())
    (run_destination / "report.json").write_text(
        json.dumps(sanitize_report(report), indent=2, sort_keys=True) + "\n"
    )
    shutil.copyfile(resources[0], run_destination / "resources.csv")
    return run_destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(sanitize_runs(args.source, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
