import json
from collections import deque
from types import SimpleNamespace

from cvbench.collector import OutputCollector
from tests.helpers import output


def _collector() -> OutputCollector:
    return OutputCollector(
        SimpleNamespace(),
        "CVBENCH_READY",
        100,
        10_000,
        100_000,
        100,
        "reject",
    )


def _line(timestamp_ns: int) -> bytes:
    return json.dumps(output(timestamp_ns).system_record).encode()


def test_immediate_output_is_pending_until_frame_send_succeeds() -> None:
    collector = _collector()
    key = ("seq", 10)
    collector.begin_frame(key, (100, 100))
    collector._consume_line(_line(10), deque())
    assert collector.snapshot()[:2] == ([], [])

    collector.finish_frame(key, delivered=True)
    records, errors, _stderr = collector.snapshot()
    assert len(records) == 1
    assert errors == []


def test_output_for_transport_failed_frame_is_rejected() -> None:
    collector = _collector()
    key = ("seq", 20)
    collector.begin_frame(key, (100, 100))
    collector._consume_line(_line(20), deque())
    collector.finish_frame(key, delivered=False)

    records, errors, _stderr = collector.snapshot()
    assert records == []
    assert any("delivery failed" in error for error in errors)


def test_output_boundary_uses_one_fixed_snapshot_and_cannot_be_extended(monkeypatch) -> None:
    collector = _collector()
    collector._stdout_fd = 9
    collector._stdout_read_bytes = 10
    pending_bytes = 64

    def snapshot(_fd, _request, pending, _mutate):
        pending[0] = pending_bytes

    monkeypatch.setattr("cvbench.collector.fcntl.ioctl", snapshot)

    assert collector.request_output_boundary() is False
    assert collector._output_boundary_target == 74
    pending_bytes = 1_000
    assert collector.request_output_boundary() is False
    assert collector._output_boundary_target == 74


def test_output_boundary_snapshot_failure_fails_closed(monkeypatch) -> None:
    collector = _collector()
    collector._stdout_fd = 9

    def fail_snapshot(*_args):
        raise OSError("unavailable")

    monkeypatch.setattr("cvbench.collector.fcntl.ioctl", fail_snapshot)

    assert collector.request_output_boundary() is False
    assert collector.flooded.is_set()
    assert collector.limit_reason == "stdout completion boundary snapshot failed: unavailable"
