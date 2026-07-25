from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Frame:
    sequence_id: str
    frame_index: int
    relative_timestamp_ns: int
    width: int
    height: int
    path: Path


@dataclass
class Scenario:
    id: str
    family: str
    root: Path
    frames: list[Frame]
    ground_truth: list[dict[str, Any]]
    faults: list[dict[str, Any]] = field(default_factory=list)
    scoreable_roi: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Match:
    sequence_id: str
    source_timestamp_ns: int
    target_id: str
    track_id: str
    gt: dict[str, Any]
    output: dict[str, Any]
    iou: float
    center_error_px: float


@dataclass
class CollectedRecord:
    collector_received_timestamp_ns: int
    system_record: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "collector_received_timestamp_ns": self.collector_received_timestamp_ns,
            "system_record": self.system_record,
        }


@dataclass
class RuntimeOutcome:
    status: str
    exit_code: int | None = None
    startup_time_ms: float | None = None
    time_to_first_output_ms: float | None = None
    errors: list[str] = field(default_factory=list)
    resolved_image: str | None = None
    timed_out: bool = False
    crashed: bool = False


@dataclass
class RunArtifacts:
    run_dir: Path
    report_json: Path
    report_html: Path
    raw_output: Path
    resources_csv: Path
