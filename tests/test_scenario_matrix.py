from pathlib import Path

from cvbench.scenario import load_scenario
from cvbench.synthetic import generate_synthetic_pack


def test_synthetic_pack_contains_exact_gap_and_target_count_matrix(tmp_path: Path) -> None:
    scenarios = [load_scenario(path) for path in generate_synthetic_pack(tmp_path / "pack")]
    gaps = set()
    target_counts = set()
    churn_births = 0
    for scenario in scenarios:
        by_timestamp: dict[int, int] = {}
        for row in scenario.ground_truth:
            if row["on_screen"] and row["eligible_for_detection"]:
                by_timestamp[row["source_timestamp_ns"]] = by_timestamp.get(row["source_timestamp_ns"], 0) + 1
        target_counts.update(by_timestamp.values())
        if scenario.family == "track_id_churn":
            churn_births = len({row["target_id"] for row in scenario.ground_truth})
        if scenario.family.startswith("occlusion_gap_"):
            rows = scenario.ground_truth
            first_hidden = min(row["source_timestamp_ns"] for row in rows if not row["eligible_for_detection"])
            first_return = min(
                row["source_timestamp_ns"]
                for row in rows
                if row["eligible_for_detection"] and row["source_timestamp_ns"] > first_hidden
            )
            gaps.add((first_return - first_hidden) // 1_000_000)
    assert gaps == {100, 250, 500, 1000, 2000}
    assert target_counts == {1, 2, 4, 8}
    assert churn_births == 48
