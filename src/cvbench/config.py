from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError

TIMING_COMPUTE_CONTRACT_VERSION = "cvbench.timing-compute/v1"
DELIVERY_POLICY_VERSION = "cvbench.delivery-lossless/v1"
REPLAY_PROFILES = {
    "quarter-speed": 0.25,
    "half-speed": 0.5,
    "native": 1.0,
    "accelerated-test-20x": 20.0,
    "accelerated-test-100x": 100.0,
}
PUBLIC_REPLAY_PROFILES = frozenset({"quarter-speed", "half-speed", "native"})


@dataclass(frozen=True)
class Thresholds:
    confirmed_track_min_duration_ms: int = 250
    visible_dropout_tolerance_ms: int = 100
    max_match_center_error_px: float = 50.0
    minimum_match_iou: float = 0.3
    ignore_match_iou: float = 0.5
    acquisition_deadlines_ms: tuple[int, ...] = (100, 250, 500, 1000)
    latency_deadline_ms: float = 250.0
    high_confidence_threshold: float = 0.8
    out_of_bounds: str = "reject"
    class_agnostic: bool = False


@dataclass(frozen=True)
class BenchmarkConfig:
    path: Path
    id: str
    version: str
    input_mode: str
    replay_profile: str
    playback_rate: float
    timing_compute_contract: str
    delivery_policy: str
    thresholds: Thresholds
    scenarios: tuple[Path, ...]
    reporting: dict[str, bool]
    resources: dict[str, Any]
    max_run_seconds: float
    max_drain_seconds: float
    max_output_records: int
    max_output_line_bytes: int
    max_total_output_bytes: int
    max_output_records_per_second: int
    long_run_assertions: dict[str, Any]
    baseline_report: Path | None
    evaluation_order_seed: str | int | None


@dataclass(frozen=True)
class SystemConfig:
    path: Path
    id: str
    revision: str
    runtime_type: str
    command: tuple[str, ...]
    image: str | None
    environment: dict[str, str]
    readiness_pattern: str
    readiness_timeout_seconds: float
    grace_period_seconds: float
    resources: dict[str, Any]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a YAML object")
    return data


def _require(data: dict[str, Any], key: str, kind: type) -> Any:
    value = data.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) and kind in (int, float):
        raise ConfigurationError(f"{key} must be {kind.__name__}")
    return value


def _mapping(data: dict[str, Any], key: str, *, name: str | None = None) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name or key} must be an object")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be boolean")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    positive: bool = False,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    if positive and result <= 0:
        raise ConfigurationError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}")
    return result


def load_benchmark(path: str | Path) -> BenchmarkConfig:
    path = Path(path).resolve()
    data = _load_yaml(path)
    if data.get("schema_version") != "cvbench.benchmark/v1":
        raise ConfigurationError("benchmark schema_version must be cvbench.benchmark/v1")
    input_config = _mapping(data, "input")
    if input_config.get("mode") not in {"online_replay", "offline_debug"}:
        raise ConfigurationError("input.mode must be online_replay or offline_debug")
    if input_config.get("protocol") != "frame_socket_v1":
        raise ConfigurationError("Version 1 input.protocol must be frame_socket_v1")
    replay_profile = input_config.get("replay_profile")
    legacy_playback_rate = input_config.get("playback_rate")
    if replay_profile is None:
        playback_rate = _number(
            1.0 if legacy_playback_rate is None else legacy_playback_rate,
            "input.playback_rate",
            positive=True,
        )
        replay_profile = next(
            (name for name, rate in REPLAY_PROFILES.items() if playback_rate == rate),
            None,
        )
        if replay_profile is None:
            allowed = ", ".join(str(rate) for rate in REPLAY_PROFILES.values())
            raise ConfigurationError(
                f"input.playback_rate must match the versioned allowlist ({allowed}); "
                "prefer input.replay_profile"
            )
    else:
        if not isinstance(replay_profile, str) or replay_profile not in REPLAY_PROFILES:
            raise ConfigurationError(
                f"input.replay_profile must be one of {', '.join(REPLAY_PROFILES)}"
            )
        playback_rate = REPLAY_PROFILES[replay_profile]
        if legacy_playback_rate is not None:
            configured_rate = _number(
                legacy_playback_rate, "input.playback_rate", positive=True
            )
            if configured_rate != playback_rate:
                raise ConfigurationError(
                    "input.playback_rate does not match input.replay_profile"
                )
    raw_thresholds = _mapping(data, "thresholds")
    out_of_bounds = raw_thresholds.get("out_of_bounds", "reject")
    if out_of_bounds not in {"reject", "clip"}:
        raise ConfigurationError("thresholds.out_of_bounds must be reject or clip")
    raw_deadlines = raw_thresholds.get("acquisition_deadlines_ms", [100, 250, 500, 1000])
    if not isinstance(raw_deadlines, list) or not raw_deadlines:
        raise ConfigurationError("thresholds.acquisition_deadlines_ms must be a non-empty integer list")
    thresholds = Thresholds(
        confirmed_track_min_duration_ms=_integer(
            raw_thresholds.get("confirmed_track_min_duration_ms", 250),
            "thresholds.confirmed_track_min_duration_ms",
            minimum=0,
        ),
        visible_dropout_tolerance_ms=_integer(
            raw_thresholds.get("visible_dropout_tolerance_ms", 100),
            "thresholds.visible_dropout_tolerance_ms",
            minimum=0,
        ),
        max_match_center_error_px=_number(
            raw_thresholds.get("max_match_center_error_px", 50),
            "thresholds.max_match_center_error_px",
            minimum=0,
        ),
        minimum_match_iou=_number(
            raw_thresholds.get("minimum_match_iou", 0.3),
            "thresholds.minimum_match_iou",
            minimum=0,
            maximum=1,
        ),
        ignore_match_iou=_number(
            raw_thresholds.get("ignore_match_iou", 0.5),
            "thresholds.ignore_match_iou",
            minimum=0,
            maximum=1,
        ),
        acquisition_deadlines_ms=tuple(
            _integer(value, "thresholds.acquisition_deadlines_ms[]", minimum=1) for value in raw_deadlines
        ),
        latency_deadline_ms=_number(
            raw_thresholds.get("latency_deadline_ms", 250),
            "thresholds.latency_deadline_ms",
            minimum=0,
        ),
        high_confidence_threshold=_number(
            raw_thresholds.get("high_confidence_threshold", 0.8),
            "thresholds.high_confidence_threshold",
            minimum=0,
            maximum=1,
        ),
        out_of_bounds=out_of_bounds,
        class_agnostic=_boolean(
            raw_thresholds.get("class_agnostic", False), "thresholds.class_agnostic"
        ),
    )
    scenario_items = data.get("scenarios")
    if not isinstance(scenario_items, list) or not scenario_items:
        raise ConfigurationError("benchmark scenarios must be a non-empty list")
    scenarios: list[Path] = []
    for item in scenario_items:
        raw_path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw_path, str):
            raise ConfigurationError("each scenario must provide a path")
        scenarios.append((path.parent / raw_path).resolve())
    reporting = _mapping(data, "reporting")
    resources = _mapping(data, "resources")
    baseline = data.get("baseline_report")
    evaluation_order_seed = data.get("evaluation_order_seed")
    if evaluation_order_seed is not None and (
        not isinstance(evaluation_order_seed, (str, int)) or isinstance(evaluation_order_seed, bool)
    ):
        raise ConfigurationError("evaluation_order_seed must be a string or integer")
    max_output_records = _integer(data.get("max_output_records", 100_000), "max_output_records", minimum=1)
    max_output_line_bytes = _integer(
        data.get("max_output_line_bytes", 1_000_000), "max_output_line_bytes", minimum=1
    )
    max_total_output_bytes = _integer(
        data.get("max_total_output_bytes", 50_000_000), "max_total_output_bytes", minimum=1
    )
    max_output_records_per_second = _integer(
        data.get("max_output_records_per_second", 10_000), "max_output_records_per_second", minimum=1
    )
    long_run_assertions = _mapping(data, "long_run_assertions")
    return BenchmarkConfig(
        path=path,
        id=_require(data, "id", str),
        version=_require(data, "version", str),
        input_mode=input_config["mode"],
        replay_profile=replay_profile,
        playback_rate=playback_rate,
        timing_compute_contract=TIMING_COMPUTE_CONTRACT_VERSION,
        delivery_policy=DELIVERY_POLICY_VERSION,
        thresholds=thresholds,
        scenarios=tuple(scenarios),
        reporting={
            "generate_json": _boolean(reporting.get("generate_json", True), "reporting.generate_json"),
            "generate_html": _boolean(reporting.get("generate_html", True), "reporting.generate_html"),
            "generate_failure_packets": _boolean(
                reporting.get("generate_failure_packets", True), "reporting.generate_failure_packets"
            ),
        },
        resources=resources,
        max_run_seconds=_number(data.get("max_run_seconds", 120), "max_run_seconds", positive=True),
        max_drain_seconds=_number(
            data.get("max_drain_seconds", 10), "max_drain_seconds", minimum=0
        ),
        max_output_records=max_output_records,
        max_output_line_bytes=max_output_line_bytes,
        max_total_output_bytes=max_total_output_bytes,
        max_output_records_per_second=max_output_records_per_second,
        long_run_assertions=long_run_assertions,
        baseline_report=(path.parent / baseline).resolve() if isinstance(baseline, str) else None,
        evaluation_order_seed=evaluation_order_seed,
    )


def load_system(path: str | Path) -> SystemConfig:
    path = Path(path).resolve()
    data = _load_yaml(path)
    if data.get("schema_version") != "cvbench.system/v1":
        raise ConfigurationError("system schema_version must be cvbench.system/v1")
    runtime = _mapping(data, "runtime")
    runtime_type = runtime.get("type")
    if runtime_type not in {"local", "docker"}:
        raise ConfigurationError("runtime.type must be local or docker")
    command = runtime.get("command", [])
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        raise ConfigurationError("runtime.command must be a non-empty string list")
    image = runtime.get("image")
    if runtime_type == "docker" and not isinstance(image, str):
        raise ConfigurationError("Docker runtime requires an image")
    environment = _mapping(runtime, "environment", name="runtime.environment")
    if not all(
        isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in environment.items()
    ):
        raise ConfigurationError("runtime.environment must contain scalar values")
    readiness = _mapping(data, "readiness")
    if readiness.get("type", "stdout_pattern") != "stdout_pattern":
        raise ConfigurationError("Version 1 readiness.type must be stdout_pattern")
    pattern = readiness.get("pattern", "CVBENCH_READY")
    if not isinstance(pattern, str) or not pattern:
        raise ConfigurationError("readiness.pattern must be a non-empty string")
    shutdown = _mapping(data, "shutdown")
    resources = _mapping(data, "resources")
    return SystemConfig(
        path=path,
        id=_require(data, "id", str),
        revision=_require(data, "revision", str),
        runtime_type=runtime_type,
        command=tuple(command),
        image=image,
        environment={str(k): str(v) for k, v in environment.items()},
        readiness_pattern=pattern,
        readiness_timeout_seconds=_number(
            readiness.get("timeout_seconds", 30), "readiness.timeout_seconds", minimum=0
        ),
        grace_period_seconds=_number(
            shutdown.get("grace_period_seconds", 10), "shutdown.grace_period_seconds", minimum=0
        ),
        resources=resources,
    )
