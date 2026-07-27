from __future__ import annotations

import html
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def _schema(name: str) -> dict[str, Any]:
    source = Path(__file__).parents[2] / "schemas" / name
    text = source.read_text() if source.exists() else resources.files("cvbench").joinpath("_schemas", name).read_text()
    return json.loads(text)


@lru_cache(maxsize=1)
def _report_validator() -> Draft202012Validator:
    timing_schema = _schema("timing-compute-v1.schema.json")
    registry = Registry().with_resource(timing_schema["$id"], Resource.from_contents(timing_schema))
    return Draft202012Validator(
        _schema("report-v1.schema.json"),
        registry=registry,
        format_checker=FormatChecker(),
    )


@lru_cache(maxsize=1)
def _redacted_report_validator() -> Draft202012Validator:
    timing_schema = _schema("timing-compute-v1.schema.json")
    report_schema = _schema("report-v1.schema.json")
    registry = Registry()
    registry = registry.with_resource(timing_schema["$id"], Resource.from_contents(timing_schema))
    registry = registry.with_resource(report_schema["$id"], Resource.from_contents(report_schema))
    return Draft202012Validator(
        _schema("report-redacted-v1.schema.json"),
        registry=registry,
        format_checker=FormatChecker(),
    )


def _validate(report: dict[str, Any], validator: Draft202012Validator, schema_version: str) -> None:
    wire_report = json.loads(json.dumps(report, allow_nan=False))
    errors = sorted(validator.iter_errors(wire_report), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<report>"
        raise ValueError(f"invalid {schema_version} at {location}: {error.message}")


def validate_report(report: dict[str, Any]) -> None:
    _validate(report, _report_validator(), "cvbench.report/v1")


def validate_redacted_report(report: dict[str, Any]) -> None:
    _validate(report, _redacted_report_validator(), "cvbench.report-redacted/v1")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _format(value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_html(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    outcome = report["outcome"]
    cards = [
        ("Run status", outcome["status"]),
        ("Acquisition rate", metrics["acquisition"].get("rate")),
        ("Observed coverage", metrics["coverage"].get("overall_observed")),
        ("Continuity", metrics["coverage"].get("overall_continuity")),
        ("Median latency (ms)", metrics["latency"].get("median")),
        (
            "Native-source offset p95 (ms)",
            report.get("timing", {}).get("native_source_offset_ms", {}).get("p95"),
        ),
        (
            "CPU-s / native source-s",
            report.get("resources", {}).get("cpu_seconds_per_native_source_second"),
        ),
        (
            "Real-time factor",
            report.get("timing", {}).get("durations", {}).get("real_time_factor"),
        ),
        ("Leaderboard class", report.get("leaderboard", {}).get("class_id")),
        ("Mean IoU", metrics["localization"].get("mean_iou")),
        ("ID switches", metrics["identity"].get("id_switches")),
        ("False track births", metrics["false_detections"].get("track_births")),
    ]
    card_html = "".join(
        f'<section class="card"><h2>{html.escape(label)}</h2><p>{html.escape(_format(value))}</p></section>'
        for label, value in cards
    )
    findings = (
        "".join(
            f"<li><strong>{html.escape(item['finding_id'])}</strong> — "
            f"{html.escape(item['interpretation']['statement'])}</li>"
            for item in report.get("findings", [])
        )
        or "<li>No significant findings.</li>"
    )
    payload = html.escape(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    title = html.escape(f"{report['benchmark']['id']} — {report['system']['id']}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CVBench report: {title}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:auto;padding:2rem;color:#17202a}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1rem}}
.card{{border:1px solid #d5d8dc;border-radius:.5rem;padding:1rem}}.card h2{{font-size:.9rem;margin:0;color:#566573}}
.card p{{font-size:1.6rem;margin:.35rem 0 0}}pre{{overflow:auto;background:#f4f6f7;padding:1rem;border-radius:.5rem}}
.status{{text-transform:uppercase;letter-spacing:.06em}}h1{{margin-bottom:.25rem}}
</style></head><body>
<h1>CVBench Benchmark</h1><p>{title}</p><p class="status">Mode: {html.escape(report["mode"])}</p>
<div class="grid">{card_html}</div><h2>Diagnostic findings</h2><ul>{findings}</ul>
<h2>Complete machine-readable report</h2><pre>{payload}</pre></body></html>"""


def write_report_files(run_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    validate_report(report)
    json_path = run_dir / "report.json"
    html_path = run_dir / "report.html"
    write_json(json_path, report)
    html_path.write_text(render_html(report))
    return json_path, html_path
