from pathlib import Path
from unittest.mock import patch

from cvbench.evidence import generate_evidence_packets


def test_identical_failure_videos_are_generated_once_per_run(tmp_path: Path) -> None:
    findings = [
        {
            "finding_id": finding_id,
            "severity": "high",
            "interpretation": {"statement": "failure"},
        }
        for finding_id in ("ONE", "TWO")
    ]

    def fake_video(path: Path, *_args: object) -> bool:
        path.write_bytes(b"video")
        return True

    with patch("cvbench.evidence._video", side_effect=fake_video) as video:
        generate_evidence_packets(tmp_path, findings, [], [], [], [], tmp_path / "missing.csv", "cvbench run")

    assert video.call_count == 2
    assert (tmp_path / "failures/ONE/input_clip.mp4").read_bytes() == b"video"
    assert (tmp_path / "failures/TWO/overlay.mp4").read_bytes() == b"video"
