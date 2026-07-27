import json
import shutil
from pathlib import Path

import pytest
import yaml

from cvbench.errors import ConfigurationError
from cvbench.scenario import load_scenario
from tests.helpers import gt

ROOT = Path(__file__).parents[1]


def _scenario_file(tmp_path: Path, **changes: object) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    frames = tmp_path / "frames"
    frames.mkdir()
    shutil.copy2(ROOT / "scenarios/synthetic-v1/acquisition/frames/0000.jpg", frames / "0000.jpg")
    manifest = {
        "schema_version": "cvbench.scenario/v1",
        "id": "scenario-test",
        "family": "test",
        "sequence_id": "seq",
        "ground_truth": "ground_truth.jsonl",
        "frames": [
            {
                "frame_index": 0,
                "source_timestamp_ns": 0,
                "width": 160,
                "height": 120,
                "path": "frames/0000.jpg",
            }
        ],
    }
    manifest.update(changes)
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(manifest))
    (tmp_path / "ground_truth.jsonl").write_text(json.dumps(gt(0)) + "\n")
    return path


def test_ground_truth_timestamp_must_match_an_exact_scenario_frame(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    shutil.copy2(ROOT / "scenarios/synthetic-v1/acquisition/frames/0000.jpg", frames / "0000.jpg")
    manifest = {
        "schema_version": "cvbench.scenario/v1",
        "id": "timestamp-mismatch",
        "family": "test",
        "sequence_id": "seq",
        "ground_truth": "ground_truth.jsonl",
        "frames": [
            {
                "frame_index": 0,
                "source_timestamp_ns": 0,
                "width": 160,
                "height": 120,
                "path": "frames/0000.jpg",
            }
        ],
    }
    (tmp_path / "scenario.yaml").write_text(yaml.safe_dump(manifest))
    (tmp_path / "ground_truth.jsonl").write_text(json.dumps(gt(1)) + "\n")

    with pytest.raises(ConfigurationError, match="timestamp does not match"):
        load_scenario(tmp_path / "scenario.yaml")


@pytest.mark.parametrize("value", [{}, "delay", None, True, 1])
def test_faults_must_be_a_list(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigurationError, match=r"^scenario faults must be a list$"):
        load_scenario(_scenario_file(tmp_path, faults=value))


@pytest.mark.parametrize("entry", ["delay", None, True, [], 1])
def test_fault_entries_must_be_objects(tmp_path: Path, entry: object) -> None:
    with pytest.raises(ConfigurationError, match=r"^scenario faults entries must be objects$"):
        load_scenario(_scenario_file(tmp_path, faults=[entry]))


@pytest.mark.parametrize("value", [{}, "frame", None, True, 1])
def test_frames_must_be_a_list(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigurationError, match=r"^scenario frames must be a list$"):
        load_scenario(_scenario_file(tmp_path, frames=value))


@pytest.mark.parametrize("entry", ["frame", None, True, [], 1])
def test_frame_entries_must_be_objects(tmp_path: Path, entry: object) -> None:
    with pytest.raises(ConfigurationError, match=r"^scenario frames entries must be objects$"):
        load_scenario(_scenario_file(tmp_path, frames=[entry]))


def test_fault_objects_and_omitted_faults_are_preserved(tmp_path: Path) -> None:
    fault = {"type": "delay", "frame_indices": [0], "duration_ms": 1}
    assert load_scenario(_scenario_file(tmp_path / "valid", faults=[fault])).faults == [fault]
    assert load_scenario(_scenario_file(tmp_path / "omitted")).faults == []
