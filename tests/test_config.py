from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cvbench.config import load_benchmark, load_system
from cvbench.errors import ConfigurationError

ROOT = Path(__file__).parents[1]


def _benchmark_file(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    data = yaml.safe_load((ROOT / "benchmarks/persistent-target-tracking.yaml").read_text())
    change(data)
    path = tmp_path / "benchmark.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _system_file(tmp_path: Path, change: Callable[[dict[str, Any]], None]) -> Path:
    data = yaml.safe_load((ROOT / "systems/example-good-local.yaml").read_text())
    change(data)
    path = tmp_path / "system.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_shipped_definitions_validate() -> None:
    benchmark = load_benchmark(ROOT / "benchmarks/persistent-target-tracking.yaml")
    system = load_system(ROOT / "systems/example-good-local.yaml")
    assert benchmark.input_mode == "online_replay"
    assert benchmark.replay_profile == "native"
    assert benchmark.playback_rate == 1.0
    assert system.runtime_type == "local"


def test_mutually_invalid_input_mode_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "schema_version: cvbench.benchmark/v1\n"
        "id: x\nversion: '1'\n"
        "input: {mode: future, protocol: frame_socket_v1}\n"
        "scenarios: [x]\n"
    )
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", ["1", float("nan"), float("inf"), 0, -1])
def test_playback_rate_must_be_typed_positive_and_finite(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data["input"].__setitem__("playback_rate", value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", [0.1, 0.75, 2, 10])
def test_legacy_playback_rate_must_match_the_versioned_allowlist(
    tmp_path: Path, value: Any
) -> None:
    path = _benchmark_file(
        tmp_path,
        lambda data: data["input"].update(
            {"playback_rate": value, "replay_profile": None}
        ),
    )
    with pytest.raises(ConfigurationError, match="versioned allowlist"):
        load_benchmark(path)


@pytest.mark.parametrize(
    ("profile", "rate"),
    [
        ("quarter-speed", 0.25),
        ("half-speed", 0.5),
        ("native", 1.0),
        ("accelerated-test-20x", 20.0),
        ("accelerated-test-100x", 100.0),
    ],
)
def test_versioned_replay_profiles_resolve_exact_rates(
    tmp_path: Path, profile: str, rate: float
) -> None:
    path = _benchmark_file(
        tmp_path,
        lambda data: data["input"].update({"replay_profile": profile}),
    )
    benchmark = load_benchmark(path)
    assert benchmark.replay_profile == profile
    assert benchmark.playback_rate == rate


def test_replay_profile_and_legacy_rate_cannot_disagree(tmp_path: Path) -> None:
    path = _benchmark_file(
        tmp_path,
        lambda data: data["input"].update(
            {"replay_profile": "half-speed", "playback_rate": 1.0}
        ),
    )
    with pytest.raises(ConfigurationError, match="does not match"):
        load_benchmark(path)


@pytest.mark.parametrize("key", ["max_run_seconds", "max_drain_seconds"])
@pytest.mark.parametrize("value", ["120", float("nan"), float("inf"), -1])
def test_run_durations_must_be_typed_finite_and_nonnegative(
    tmp_path: Path, key: str, value: Any
) -> None:
    path = _benchmark_file(tmp_path, lambda data: data.__setitem__(key, value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", [0, -1])
def test_max_run_seconds_must_be_positive(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(
        tmp_path, lambda data: data.__setitem__("max_run_seconds", value)
    )
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", [True, False, [], {}, 1.5])
def test_evaluation_order_seed_rejects_bool_and_other_non_seed_types(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data.__setitem__("evaluation_order_seed", value))
    with pytest.raises(ConfigurationError, match="evaluation_order_seed"):
        load_benchmark(path)


@pytest.mark.parametrize("value", [None, "seed-1", 17])
def test_evaluation_order_seed_accepts_supported_types(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data.__setitem__("evaluation_order_seed", value))
    assert load_benchmark(path).evaluation_order_seed == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("minimum_match_iou", -0.01),
        ("minimum_match_iou", 1.01),
        ("minimum_match_iou", "0.5"),
        ("high_confidence_threshold", -0.01),
        ("high_confidence_threshold", 1.01),
        ("high_confidence_threshold", float("nan")),
        ("max_match_center_error_px", -1),
        ("max_match_center_error_px", "50"),
        ("latency_deadline_ms", float("inf")),
        ("latency_deadline_ms", -1),
        ("confirmed_track_min_duration_ms", "250"),
        ("visible_dropout_tolerance_ms", -1),
    ],
)
def test_thresholds_are_typed_finite_and_bounded(tmp_path: Path, name: str, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data["thresholds"].__setitem__(name, value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_class_agnostic_requires_a_real_boolean(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data["thresholds"].__setitem__("class_agnostic", value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("value", [[], [0], ["100"], [True]])
def test_acquisition_deadlines_are_positive_integer_values(tmp_path: Path, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data["thresholds"].__setitem__("acquisition_deadlines_ms", value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize(
    "key",
    ["max_output_records", "max_output_line_bytes", "max_total_output_bytes", "max_output_records_per_second"],
)
@pytest.mark.parametrize("value", ["10", 0, -1, True])
def test_output_limits_are_positive_integers(tmp_path: Path, key: str, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data.__setitem__(key, value))
    with pytest.raises(ConfigurationError):
        load_benchmark(path)


@pytest.mark.parametrize("section", ["input", "thresholds", "reporting", "resources", "long_run_assertions"])
@pytest.mark.parametrize("value", [[], "invalid", None, True])
def test_benchmark_mapping_sections_reject_non_objects(tmp_path: Path, section: str, value: Any) -> None:
    path = _benchmark_file(tmp_path, lambda data: data.__setitem__(section, value))
    with pytest.raises(ConfigurationError, match=rf"^{section} must be an object$"):
        load_benchmark(path)


@pytest.mark.parametrize("section", ["runtime", "readiness", "shutdown", "resources"])
@pytest.mark.parametrize("value", [[], "invalid", None, True])
def test_system_mapping_sections_reject_non_objects(tmp_path: Path, section: str, value: Any) -> None:
    path = _system_file(tmp_path, lambda data: data.__setitem__(section, value))
    with pytest.raises(ConfigurationError, match=rf"^{section} must be an object$"):
        load_system(path)


@pytest.mark.parametrize("value", [[], "invalid", None, True])
def test_runtime_environment_rejects_non_objects(tmp_path: Path, value: Any) -> None:
    path = _system_file(tmp_path, lambda data: data["runtime"].__setitem__("environment", value))
    with pytest.raises(ConfigurationError, match=r"^runtime\.environment must be an object$"):
        load_system(path)


def test_optional_mapping_sections_accept_omitted_defaults(tmp_path: Path) -> None:
    def omit_benchmark_sections(data: dict[str, Any]) -> None:
        for section in ("thresholds", "reporting", "resources", "long_run_assertions"):
            data.pop(section, None)

    benchmark = load_benchmark(_benchmark_file(tmp_path, omit_benchmark_sections))
    assert benchmark.reporting == {
        "generate_json": True,
        "generate_html": True,
        "generate_failure_packets": True,
    }
    assert benchmark.resources == {}
    assert benchmark.long_run_assertions == {}

    def omit_system_sections(data: dict[str, Any]) -> None:
        data["runtime"].pop("environment", None)
        for section in ("readiness", "shutdown", "resources"):
            data.pop(section, None)

    system = load_system(_system_file(tmp_path, omit_system_sections))
    assert system.environment == {}
    assert system.resources == {}
    assert system.readiness_pattern == "CVBENCH_READY"
    assert system.grace_period_seconds == 10


@pytest.mark.parametrize("section,key", [("readiness", "timeout_seconds"), ("shutdown", "grace_period_seconds")])
@pytest.mark.parametrize("value", ["1", float("nan"), float("inf"), -1])
def test_system_durations_are_typed_finite_and_nonnegative(
    tmp_path: Path, section: str, key: str, value: Any
) -> None:
    path = _system_file(tmp_path, lambda data: data.setdefault(section, {}).__setitem__(key, value))
    with pytest.raises(ConfigurationError):
        load_system(path)


def test_numeric_boundaries_are_accepted(tmp_path: Path) -> None:
    def update_benchmark(data: dict[str, Any]) -> None:
        data["input"]["replay_profile"] = "quarter-speed"
        data["max_run_seconds"] = 0.001
        data["max_drain_seconds"] = 0
        data["thresholds"].update(
            {
                "confirmed_track_min_duration_ms": 0,
                "visible_dropout_tolerance_ms": 0,
                "max_match_center_error_px": 0,
                "minimum_match_iou": 0,
                "latency_deadline_ms": 0,
                "high_confidence_threshold": 1,
                "class_agnostic": False,
            }
        )

    benchmark = load_benchmark(_benchmark_file(tmp_path, update_benchmark))
    assert benchmark.playback_rate == 0.25
    assert benchmark.max_drain_seconds == 0
    assert benchmark.thresholds.minimum_match_iou == 0

    def update_system(data: dict[str, Any]) -> None:
        data["readiness"]["timeout_seconds"] = 0
        data["shutdown"]["grace_period_seconds"] = 0

    system = load_system(_system_file(tmp_path, update_system))
    assert system.readiness_timeout_seconds == 0
    assert system.grace_period_seconds == 0
