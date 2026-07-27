from dataclasses import replace
from pathlib import Path

from cvbench.comparison import compare_reports
from cvbench.config import load_benchmark
from cvbench.runner import (
    EVALUATION_ORDER_ALGORITHM,
    _comparison_fingerprint,
    _load_unique_scenarios,
    _portable_path,
)
from cvbench.scenario import load_scenario

ROOT = Path(__file__).parents[1]


def test_baseline_comparison_labels_direction_and_low_confidence() -> None:
    baseline = {
        "metrics": {"acquisition": {"rate": 0.5}, "sample_counts": {"matches": 10}},
        "resources": {"peak_ram_bytes": 200},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"eligible": True, "class_id": "native/cpu-1/realtime"},
    }
    candidate = {
        "metrics": {"acquisition": {"rate": 0.8}, "sample_counts": {"matches": 10}},
        "resources": {"peak_ram_bytes": 150},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"eligible": True, "class_id": "native/cpu-1/realtime"},
    }
    results = {item["metric"]: item for item in compare_reports(baseline, candidate)}
    assert results["acquisition.rate"]["direction"] == "improvement"
    assert results["acquisition.rate"]["confidence"] == "low"
    assert results["resources.peak_ram_bytes"]["direction"] == "improvement"


def test_incompatible_scenario_comparison_is_inconclusive() -> None:
    baseline = {"metrics": {"acquisition": {"rate": 0.5}}, "provenance": {"comparison_fingerprint": "a"}}
    candidate = {"metrics": {"acquisition": {"rate": 0.9}}, "provenance": {"comparison_fingerprint": "b"}}
    results = compare_reports(baseline, candidate)
    assert results
    assert all(result["direction"] == "inconclusive" for result in results)


def test_different_compute_or_completion_classes_are_never_equal_budget_comparisons() -> None:
    baseline = {
        "metrics": {"acquisition": {"rate": 0.5}},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"class_id": "native/cpu-1/realtime"},
    }
    candidate = {
        "metrics": {"acquisition": {"rate": 0.9}},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"class_id": "native/cpu-4/completion-2x"},
    }
    results = compare_reports(baseline, candidate)
    assert results
    assert all(result["direction"] == "inconclusive" for result in results)
    assert all("leaderboard classes" in result["reason"] for result in results)


def test_ineligible_reports_never_receive_equal_budget_comparisons() -> None:
    baseline = {
        "metrics": {"acquisition": {"rate": 0.5}},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"eligible": True, "class_id": "native/cpu-1/realtime"},
    }
    candidate = {
        "metrics": {"acquisition": {"rate": 0.9}},
        "provenance": {"comparison_fingerprint": "same"},
        "leaderboard": {"eligible": False, "class_id": "native/cpu-1/realtime"},
    }
    assert all(
        result["direction"] == "inconclusive"
        for result in compare_reports(baseline, candidate)
    )


def test_comparison_fingerprint_is_independent_of_private_delivery_order() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    scenarios = [load_scenario(path) for path in benchmark.scenarios]
    first_fingerprint, first_inputs = _comparison_fingerprint(benchmark, scenarios)
    delivery_orders = [
        scenarios,
        list(reversed(scenarios)),
        [*scenarios[3:], *scenarios[:3]],
        [scenarios[index] for index in (0, 4, 8, 1, 5, 9, 2, 6, 10, 3, 7, 11)],
    ]
    for shuffled in delivery_orders:
        second_fingerprint, second_inputs = _comparison_fingerprint(benchmark, shuffled)
        assert second_fingerprint == first_fingerprint
        assert second_inputs == first_inputs

    changed_corpus = [replace(scenarios[0], id=f"{scenarios[0].id}-changed"), *scenarios[1:]]
    changed_config = replace(benchmark, version="changed")
    changed_budget = replace(benchmark, max_drain_seconds=benchmark.max_drain_seconds + 1)
    assert _comparison_fingerprint(benchmark, changed_corpus)[0] != first_fingerprint
    assert _comparison_fingerprint(changed_config, shuffled)[0] != first_fingerprint
    assert _comparison_fingerprint(changed_budget, scenarios)[0] != first_fingerprint
    assert _comparison_fingerprint(
        benchmark,
        scenarios,
        {"cpu_limit": 4, "memory_limit_mb": 2048},
        {"external_cgroup_v2": True},
    )[0] != _comparison_fingerprint(
        benchmark,
        scenarios,
        {"cpu_limit": 2, "memory_limit_mb": 2048},
        {"external_cgroup_v2": True},
    )[0]


def test_run_scoped_order_still_has_stable_comparison_fingerprint() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    first = _load_unique_scenarios(benchmark.scenarios, "run-a-11111111")
    second = _load_unique_scenarios(benchmark.scenarios, "run-b-22222222")
    assert _comparison_fingerprint(benchmark, first)[0] == _comparison_fingerprint(benchmark, second)[0]


def test_configured_order_seed_is_honored_and_fingerprinted() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    configured = replace(benchmark, evaluation_order_seed=17)
    first = _load_unique_scenarios(configured.scenarios, "run-a-11111111", configured.evaluation_order_seed)
    second = _load_unique_scenarios(configured.scenarios, "run-b-22222222", configured.evaluation_order_seed)
    assert [scenario.id for scenario in first] == [scenario.id for scenario in second]
    first_fingerprint, inputs = _comparison_fingerprint(configured, first)
    fallback_fingerprint, fallback_inputs = _comparison_fingerprint(benchmark, first)
    assert first_fingerprint != fallback_fingerprint
    assert inputs["evaluation_order"] == {
        "algorithm": EVALUATION_ORDER_ALGORITHM,
        "mode": "configured_seed",
        "seed": 17,
    }
    assert fallback_inputs["evaluation_order"] == {
        "algorithm": EVALUATION_ORDER_ALGORITHM,
        "mode": "private_per_run_fallback",
        "seed": None,
    }


def test_versioned_order_has_expected_vectors_for_seed_and_private_run() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    configured = replace(benchmark, evaluation_order_seed=17)
    configured_order = [scenario.id for scenario in _load_unique_scenarios(configured.scenarios, "run-a-11111111", 17)]
    assert configured_order == [
        "synthetic-resource-stress",
        "synthetic-occlusion-gap-2000ms",
        "synthetic-multi-target-pair",
        "synthetic-visible-retention",
        "synthetic-occlusion-gap-250ms",
        "synthetic-multi-target-identity",
        "synthetic-occlusion-gap-100ms",
        "synthetic-occlusion-reacquisition",
        "synthetic-occlusion-gap-1000ms",
        "synthetic-occlusion-gap-500ms",
        "synthetic-acquisition",
        "synthetic-false-detection",
    ]
    private_a = [scenario.id for scenario in _load_unique_scenarios(benchmark.scenarios, "run-a-11111111")]
    private_b = [scenario.id for scenario in _load_unique_scenarios(benchmark.scenarios, "run-b-22222222")]
    configured_other_seed = [
        scenario.id for scenario in _load_unique_scenarios(configured.scenarios, "run-a-11111111", 18)
    ]
    assert configured_other_seed != configured_order
    assert private_a == [
        "synthetic-occlusion-gap-100ms",
        "synthetic-multi-target-pair",
        "synthetic-occlusion-gap-1000ms",
        "synthetic-occlusion-gap-2000ms",
        "synthetic-occlusion-gap-500ms",
        "synthetic-visible-retention",
        "synthetic-acquisition",
        "synthetic-resource-stress",
        "synthetic-false-detection",
        "synthetic-multi-target-identity",
        "synthetic-occlusion-reacquisition",
        "synthetic-occlusion-gap-250ms",
    ]
    assert private_b != private_a
    assert [scenario.id for scenario in _load_unique_scenarios(configured.scenarios, "run-b-22222222", 17)] == [
        "synthetic-resource-stress",
        "synthetic-occlusion-gap-2000ms",
        "synthetic-multi-target-pair",
        "synthetic-visible-retention",
        "synthetic-occlusion-gap-250ms",
        "synthetic-multi-target-identity",
        "synthetic-occlusion-gap-100ms",
        "synthetic-occlusion-reacquisition",
        "synthetic-occlusion-gap-1000ms",
        "synthetic-occlusion-gap-500ms",
        "synthetic-acquisition",
        "synthetic-false-detection",
    ]


def test_published_provenance_paths_are_portable() -> None:
    assert _portable_path(Path("/Users/alice/worktree/scenarios/rv1/scenario.yaml")) == "scenarios/rv1/scenario.yaml"
    assert _portable_path(Path("/private/tmp/run-output")) == "run-output"
