import subprocess

from cvbench.examples.good_tracker import (
    _center,
    _clamp_box,
    _stop_background_worker,
)


def test_coasting_box_is_clamped_to_frame_bounds_and_remains_nonempty() -> None:
    box = _clamp_box([-20.0, 115.0, -5.0, 140.0], 160, 120)
    assert box == [0.0, 115.0, 1.0, 120.0]
    center = _center(box)
    assert 0 <= center[0] <= 160
    assert 0 <= center[1] <= 120


class _IgnoringWorker:
    def __init__(self):
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        if self.calls.count(("wait", timeout)) == 1:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        return 0

    def kill(self):
        self.calls.append("kill")


def test_background_worker_has_kill_fallback() -> None:
    worker = _IgnoringWorker()
    _stop_background_worker(worker)
    assert worker.calls == [
        "terminate",
        ("wait", 2),
        "kill",
        ("wait", 2),
    ]
