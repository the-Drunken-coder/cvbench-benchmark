from __future__ import annotations

from pathlib import Path

from cvbench.config import Thresholds
from cvbench.metrics import calculate_metrics
from cvbench.reporting import write_json
from tests.helpers import gt, output

ROOT = Path(__file__).parents[1]


def known_report() -> dict:
    ground_truth = [
        gt(1_000_000_000, sequence="acquisition"),
        gt(1_220_000_000, sequence="acquisition"),
        gt(3_900_000_000, sequence="acquisition"),
        gt(4_000_000_000, sequence="acquisition"),
        gt(4_100_000_000, sequence="acquisition"),
        gt(4_200_000_000, sequence="acquisition"),
        gt(4_300_000_000, sequence="acquisition"),
        gt(4_350_000_000, sequence="acquisition"),
        gt(10_000_000_000, sequence="reacquisition"),
        gt(10_100_000_000, sequence="reacquisition", eligible=False, visibility=0, occlusion="full"),
        gt(10_200_000_000, sequence="reacquisition", eligible=False, visibility=0, occlusion="full"),
        gt(10_300_000_000, sequence="reacquisition"),
        gt(10_480_000_000, sequence="reacquisition"),
        gt(10_580_000_000, sequence="reacquisition"),
        gt(20_000_000_000, sequence="false", box=[100, 100, 110, 110]),
        gt(21_000_000_000, sequence="false", box=[100, 100, 110, 110]),
    ]
    records = [
        output(1_220_000_000, sequence="acquisition"),
        output(3_900_000_000, sequence="acquisition"),
        output(4_350_000_000, sequence="acquisition"),
        output(10_000_000_000, sequence="reacquisition", track="same"),
        output(10_100_000_000, sequence="reacquisition", track="same", support="predicted", state="coasting"),
        output(10_200_000_000, sequence="reacquisition", track="same", support="predicted", state="coasting"),
        output(10_480_000_000, sequence="reacquisition", track="same", state="reacquired"),
        output(20_000_000_000, sequence="false", track="false", box=[0, 0, 10, 10]),
        output(21_000_000_000, sequence="false", track="false", box=[0, 0, 10, 10]),
    ]
    metrics, _ = calculate_metrics(
        ground_truth,
        records,
        Thresholds(visible_dropout_tolerance_ms=0, max_match_center_error_px=5),
    )
    return {
        "schema_version": "cvbench.report/v1",
        "run_id": "golden-known-scenario",
        "started_at": "2026-01-01T00:00:00+00:00",
        "mode": "online_replay",
        "benchmark": {"id": "golden", "version": "1"},
        "system": {"id": "fixture", "revision": "known", "runtime": "local"},
        "outcome": {"status": "completed", "exit_code": 0},
        "feed": {"delivered_frames": 16, "dropped_frames": 0},
        "metrics": metrics,
        "resources": {"sample_count": 1, "peak_ram_bytes": 1024, "average_cpu_percent": 5.0},
        "runtime_isolation": {"runtime": "local", "status": "not_enforced_local"},
        "findings": [],
        "comparison": [],
        "provenance": {"external_clock": "time.monotonic_ns", "comparison_fingerprint": "golden"},
        "diagnostics": {"collector_errors": [], "match_count": metrics["sample_counts"]["matches"]},
        "limitations": [],
    }


def test_complete_known_report_matches_golden(tmp_path: Path) -> None:
    actual = tmp_path / "report.json"
    write_json(actual, known_report())
    assert actual.read_text() == (ROOT / "tests/golden/known-report.json").read_text()
