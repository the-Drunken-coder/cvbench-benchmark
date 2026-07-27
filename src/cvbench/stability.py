from __future__ import annotations

from typing import Any


def evaluate_long_run_assertions(
    observations: dict[str, Any], resources: dict[str, Any], declared: dict[str, Any]
) -> dict[str, Any]:
    result = dict(observations)
    result["memory_growth_bytes"] = resources.get("memory_growth_bytes")
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, actual: Any, passed: bool | None) -> None:
        checks[name] = {
            "actual": actual,
            "limit": declared[name],
            "evaluated": actual is not None,
            "passed": passed if actual is not None else None,
        }

    def maximum(name: str, field: str, *, absolute: bool = False) -> None:
        if name not in declared:
            return
        actual = result.get(field)
        compared = abs(actual) if absolute and actual is not None else actual
        check(name, actual, compared <= declared[name] if compared is not None else None)

    maximum("max_unique_track_ids", "unique_track_ids")
    maximum("max_track_id_reuse_events", "track_id_reuse_events")
    maximum("max_state_contamination_events", "state_contamination_events")
    maximum(
        "max_false_positive_accumulation_per_camera_minute",
        "false_positive_accumulation_per_camera_minute",
    )
    maximum("max_absolute_latency_drift_ms", "latency_drift_ms", absolute=True)
    maximum("max_memory_growth_bytes", "memory_growth_bytes")
    if "min_distinct_physical_target_births" in declared:
        actual = result.get("distinct_physical_target_births")
        check(
            "min_distinct_physical_target_births",
            actual,
            actual >= declared["min_distinct_physical_target_births"] if actual is not None else None,
        )
    if "min_interruption_recovery_rate" in declared:
        actual = result.get("interruption_recovery_rate")
        check(
            "min_interruption_recovery_rate",
            actual,
            actual >= declared["min_interruption_recovery_rate"] if actual is not None else None,
        )
    if declared.get("require_no_track_id_exhaustion"):
        actual = result.get("track_id_exhaustion_detected")
        check("require_no_track_id_exhaustion", actual, actual is False if actual is not None else None)
        checks["require_no_track_id_exhaustion"]["limit"] = False
    result["assertions"] = checks
    result["assertions_declared"] = bool(checks)
    evaluated = [item for item in checks.values() if item["evaluated"]]
    result["assertions_evaluated"] = len(evaluated)
    result["assertions_unavailable"] = sorted(name for name, item in checks.items() if not item["evaluated"])
    result["passed"] = all(item["passed"] for item in evaluated) if evaluated else None
    return result
