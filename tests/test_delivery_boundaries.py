import socket
import threading
import time
from types import SimpleNamespace

import pytest

from cvbench.model import Frame, Scenario
from cvbench.runner import _deliver_scenarios, _send_before_deadline
from cvbench.timing import DeliveryRecorder


class _Collector:
    flooded = threading.Event()
    limit_reason = None

    def begin_frame(self, _key, _size):
        pass

    def finish_frame(self, _key, *, delivered):
        assert delivered


class _Monitor:
    def set_context(self, *_args, **_kwargs):
        pass


class _Connection:
    def __init__(self):
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout


def test_benchmark_end_boundary_is_after_blocking_marker_send(tmp_path, monkeypatch) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"frame")
    scenario = Scenario(
        "scenario",
        "test",
        tmp_path,
        [Frame("sequence", 0, 0, 10, 10, frame_path)],
        [],
    )
    config = SimpleNamespace(
        input_mode="online_replay",
        timing_compute_contract="cvbench.timing-compute/v1",
        delivery_policy="cvbench.delivery-lossless/v1",
        replay_profile="native",
        playback_rate=1.0,
    )
    recorder = DeliveryRecorder(
        config.delivery_policy,
        config.replay_profile,
        config.playback_rate,
    )
    marker_returned_ns = None

    def blocking_send(_connection, metadata, _payload=b""):
        nonlocal marker_returned_ns
        if metadata["event"] == "benchmark_end":
            assert recorder.benchmark_end_sent_ns is None
            time.sleep(0.02)
            marker_returned_ns = time.monotonic_ns()

    monkeypatch.setattr("cvbench.runner.send_message", blocking_send)
    _deliver_scenarios(
        _Connection(),
        [scenario],
        config,
        time.monotonic() + 1,
        _Monitor(),
        _Collector(),
        {},
        recorder,
    )

    assert marker_returned_ns is not None
    assert recorder.benchmark_end_sent_ns >= marker_returned_ns
    assert recorder.summary()["benchmark_end_sender_call_ms"] >= 20


@pytest.mark.parametrize(
    ("metadata", "payload"),
    [
        ({"event": "stream_start"}, b""),
        ({"event": "frame"}, b"x"),
        ({"event": "frame", "duplicate": True}, b"x"),
        ({"event": "feed_interruption_start"}, b""),
        ({"event": "feed_interruption_end"}, b""),
        ({"event": "stream_end"}, b""),
        ({"event": "benchmark_end"}, b""),
    ],
)
def test_every_send_type_is_bounded_by_the_remaining_run_budget(metadata, payload) -> None:
    writer, reader = socket.socketpair()
    try:
        writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        writer.setblocking(False)
        while True:
            try:
                writer.send(b"x" * 65536)
            except BlockingIOError:
                break
        writer.setblocking(True)
        started = time.monotonic()
        deadline = started + 0.03

        with pytest.raises(TimeoutError, match="deadline expired"):
            _send_before_deadline(writer, metadata, payload, deadline)

        elapsed = time.monotonic() - started
        assert elapsed < 0.15
        assert 0 < writer.gettimeout() <= 0.03
    finally:
        writer.close()
        reader.close()


def test_delivery_sets_a_fresh_deadline_timeout_before_every_send(tmp_path, monkeypatch) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"frame")
    scenario = Scenario(
        "scenario",
        "test",
        tmp_path,
        [Frame("sequence", 0, 0, 10, 10, frame_path)],
        [],
        faults=[
            {"type": "feed_interruption", "after_frame": 0, "duration_ms": 0},
            {"type": "duplicate", "frame_indices": [0]},
        ],
    )
    config = SimpleNamespace(
        input_mode="online_replay",
        timing_compute_contract="cvbench.timing-compute/v1",
        delivery_policy="cvbench.delivery-lossless/v1",
        replay_profile="native",
        playback_rate=1.0,
    )
    recorder = DeliveryRecorder(
        config.delivery_policy,
        config.replay_profile,
        config.playback_rate,
    )
    connection = _Connection()
    observed = []

    def record_send(_connection, metadata, _payload=b""):
        observed.append((metadata["event"], metadata.get("duplicate", False), connection.timeout))

    monkeypatch.setattr("cvbench.runner.send_message", record_send)
    _deliver_scenarios(
        connection,
        [scenario],
        config,
        time.monotonic() + 1,
        _Monitor(),
        _Collector(),
        {},
        recorder,
    )

    assert [(event, duplicate) for event, duplicate, _timeout in observed] == [
        ("stream_start", False),
        ("feed_interruption_start", False),
        ("feed_interruption_end", False),
        ("frame", False),
        ("frame", True),
        ("stream_end", False),
        ("benchmark_end", False),
    ]
    assert all(timeout is not None and 0 < timeout <= 1 for _, _, timeout in observed)
