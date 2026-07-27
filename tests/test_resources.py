import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cvbench.resources import (
    ResourceMonitor,
    cgroup_v2_path,
    parse_cpu_stat,
    parse_docker_stats,
    parse_io_stat,
    parse_size,
)


def _retained_cgroup_monitor(
    tmp_path, *, process=None, interval_seconds: float = 0.1
) -> tuple[ResourceMonitor, Path]:
    cgroup_root = tmp_path / "cgroup"
    parent = cgroup_root / "cvbench-run"
    parent.mkdir(parents=True)
    for name, value in {
        "cpu.stat": "usage_usec 1000000\n",
        "io.stat": "8:0 rbytes=10 wbytes=20\n",
        "memory.current": "1024\n",
        "memory.max": "2048\n",
        "memory.peak": "1536\n",
        "pids.current": "1\n",
    }.items():
        (parent / name).write_text(value)
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    return (
        ResourceMonitor(
            process or SimpleNamespace(pid=1),
            interval_seconds=interval_seconds,
            cidfile=cidfile,
            cgroup_root=cgroup_root,
            cgroup_parent_name="cvbench-run",
            configured_cgroup_path=parent,
        ),
        parent,
    )


def test_resource_size_parser() -> None:
    assert parse_size("1.5MiB") == 1.5 * 1024**2
    assert parse_size("2 GB") == 2_000_000_000
    with pytest.raises(ValueError):
        parse_size("not-a-size")


def test_docker_resource_data_parser() -> None:
    parsed = parse_docker_stats(
        {
            "CPUPerc": "12.5%",
            "MemUsage": "10MiB / 1GiB",
            "NetIO": "2kB / 3kB",
            "BlockIO": "4MB / 5MB",
            "PIDs": "7",
        }
    )
    assert parsed["cpu_percent"] == 12.5
    assert parsed["memory_bytes"] == 10 * 1024**2
    assert parsed["network_tx_bytes"] == 3000
    assert parsed["process_count"] == 7


def test_cgroup_cpu_time_parser() -> None:
    assert parse_cpu_stat("usage_usec 1250000\nuser_usec 1000000\n") == 1.25
    assert parse_cpu_stat("usage_nsec 2500000000\n") == 2.5
    assert parse_cpu_stat("user_usec 1\n") is None


def test_external_cgroup_v2_accounting_never_executes_in_submitted_image(
    tmp_path, monkeypatch
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    (proc_root / "123").mkdir(parents=True)
    (proc_root / "123" / "cgroup").write_text("0::/docker/test\n")
    group = cgroup_root / "docker/test"
    group.mkdir(parents=True)
    (group / "cpu.stat").write_text("usage_usec 1000000\n")
    (group / "io.stat").write_text("8:0 rbytes=10 wbytes=20 rios=1 wios=1\n")
    (group / "memory.current").write_text("1024\n")
    (group / "memory.max").write_text("2048\n")
    (group / "memory.peak").write_text("1536\n")
    (group / "pids.current").write_text("3\n")
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="123\n", stderr="")

    monkeypatch.setattr("cvbench.resources.subprocess.run", fake_run)
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1),
        cidfile=cidfile,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
    )
    assert monitor.capture_checkpoint()
    (group / "cpu.stat").write_text("usage_usec 1200000\n")
    assert monitor.capture_checkpoint()
    (group / "cpu.stat").write_text("usage_usec 1500000\n")
    (group / "io.stat").write_text("8:0 rbytes=30 wbytes=80 rios=2 wios=3\n")
    assert monitor.finalize_accounting()
    assert monitor.finalize_accounting()
    summary = monitor.summary(1)

    assert cgroup_v2_path("0::/docker/test\n", cgroup_root) == group
    assert parse_io_stat("8:0 rbytes=4 wbytes=5\n") == (4, 5)
    assert summary["cpu_time_seconds"] == 1.5
    assert summary["disk_read_bytes"] == 30
    assert summary["disk_write_bytes"] == 80
    assert summary["peak_ram_bytes"] == 1536
    assert summary["accounting_scope"] == "container_cgroup_v2_external"
    assert summary["authoritative"] is True
    assert all(sample.get("accounting_source") == "host_cgroup_v2" for sample in summary["over_time"])
    assert [sample.get("final_cumulative", False) for sample in summary["over_time"]] == [
        False,
        False,
        True,
    ]
    assert commands == [["docker", "inspect", "--format", "{{.State.Pid}}", "container-id"]]
    assert not any("exec" in command for command in commands)


def test_final_accounting_never_certifies_a_stale_sample_after_cgroup_disappears(
    tmp_path, monkeypatch
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    (proc_root / "123").mkdir(parents=True)
    (proc_root / "123" / "cgroup").write_text("0::/docker/test\n")
    group = cgroup_root / "docker/test"
    group.mkdir(parents=True)
    (group / "cpu.stat").write_text("usage_usec 1000000\n")
    (group / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n")
    (group / "memory.current").write_text("1024\n")
    (group / "memory.max").write_text("2048\n")
    (group / "memory.peak").write_text("1536\n")
    (group / "pids.current").write_text("1\n")
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    monkeypatch.setattr(
        "cvbench.resources.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="123\n", stderr=""),
    )
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1),
        cidfile=cidfile,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
    )
    assert monitor.capture_checkpoint()

    # Work occurs after the genuine sample, but Docker removes the cgroup before
    # the scoring-boundary read can observe its cumulative CPU and I/O totals.
    (group / "cpu.stat").write_text("usage_usec 2500000\n")
    (group / "io.stat").write_text("8:0 rbytes=30 wbytes=4096\n")
    shutil.rmtree(group)

    assert monitor.finalize_accounting() is False
    assert monitor.finalize_accounting() is False
    assert monitor.capture_checkpoint() is False
    summary = monitor.summary(1)
    assert summary["cpu_time_seconds"] == 1
    assert summary["disk_write_bytes"] == 20
    assert summary["accounting_availability"]["final_cumulative_cpu_sample"] is False
    assert summary["authoritative"] is False
    assert not any(sample.get("final_cumulative") for sample in summary["over_time"])


def test_retained_parent_cgroup_captures_work_after_immediate_child_exit(
    tmp_path, monkeypatch
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    child = cgroup_root / "cvbench-run/container-id"
    child.mkdir(parents=True)
    parent = child.parent
    (proc_root / "123").mkdir(parents=True)
    (proc_root / "123" / "cgroup").write_text("0::/cvbench-run/container-id\n")
    (parent / "cpu.stat").write_text("usage_usec 1000000\n")
    (parent / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n")
    (parent / "memory.current").write_text("1024\n")
    (parent / "memory.max").write_text("2048\n")
    (parent / "memory.peak").write_text("1536\n")
    (parent / "pids.current").write_text("2\n")
    (child / "cpu.stat").write_text("usage_usec 1000000\n")
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    monkeypatch.setattr(
        "cvbench.resources.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="123\n", stderr=""),
    )
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1),
        cidfile=cidfile,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        cgroup_parent_name="cvbench-run",
        configured_cgroup_path=parent,
    )

    assert monitor.capture_checkpoint()
    (parent / "cpu.stat").write_text("usage_usec 2750000\n")
    (parent / "io.stat").write_text("8:0 rbytes=40 wbytes=8192\n")
    (parent / "memory.peak").write_text("1900\n")
    shutil.rmtree(child)
    shutil.rmtree(proc_root / "123")

    assert monitor.finalize_accounting()
    summary = monitor.summary(1)
    assert monitor.accounting_cgroup_path == parent
    assert summary["cpu_time_seconds"] == 2.75
    assert summary["disk_read_bytes"] == 40
    assert summary["disk_write_bytes"] == 8192
    assert summary["peak_ram_bytes"] == 1900
    assert summary["over_time"][-1]["final_cumulative"] is True
    assert summary["authoritative"] is True


def test_disappearing_retained_parent_fails_closed_at_final_boundary(tmp_path) -> None:
    cgroup_root = tmp_path / "cgroup"
    parent = cgroup_root / "cvbench-run"
    parent.mkdir(parents=True)
    (parent / "cpu.stat").write_text("usage_usec 1000000\n")
    (parent / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n")
    (parent / "memory.current").write_text("1024\n")
    (parent / "memory.max").write_text("2048\n")
    (parent / "memory.peak").write_text("1536\n")
    (parent / "pids.current").write_text("1\n")
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1),
        cidfile=cidfile,
        cgroup_root=cgroup_root,
        cgroup_parent_name="cvbench-run",
        configured_cgroup_path=parent,
    )

    assert monitor.capture_checkpoint()
    shutil.rmtree(parent)

    assert monitor.finalize_accounting() is False
    summary = monitor.summary(1)
    assert summary["accounting_availability"]["final_cumulative_cpu_sample"] is False
    assert summary["authoritative"] is False
    assert not any(sample.get("final_cumulative") for sample in summary["over_time"])


def test_final_accounting_stops_sampler_before_certified_sample(tmp_path) -> None:
    cgroup_root = tmp_path / "cgroup"
    parent = cgroup_root / "cvbench-run"
    parent.mkdir(parents=True)
    (parent / "cpu.stat").write_text("usage_usec 1000000\n")
    (parent / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n")
    (parent / "memory.current").write_text("1024\n")
    (parent / "memory.max").write_text("2048\n")
    (parent / "memory.peak").write_text("1536\n")
    (parent / "pids.current").write_text("1\n")
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("container-id")
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1, poll=lambda: None),
        interval_seconds=0.01,
        cidfile=cidfile,
        cgroup_root=cgroup_root,
        cgroup_parent_name="cvbench-run",
        configured_cgroup_path=parent,
    )
    monitor.start()
    deadline = time.monotonic() + 1
    while not monitor.samples and time.monotonic() < deadline:
        time.sleep(0.01)

    assert monitor.samples
    (parent / "cpu.stat").write_text("usage_usec 1500000\n")
    assert monitor.finalize_accounting()
    sample_count = len(monitor.samples)
    assert monitor.samples[-1]["final_cumulative"] is True
    (parent / "cpu.stat").write_text("usage_usec 2000000\n")
    time.sleep(0.05)

    assert len(monitor.samples) == sample_count
    assert monitor.summary(1)["cpu_time_seconds"] == 1.5


def test_sampler_admitted_before_capture_lock_cannot_append_after_final(
    tmp_path, monkeypatch
) -> None:
    monitor, parent = _retained_cgroup_monitor(
        tmp_path,
        process=SimpleNamespace(pid=1, poll=lambda: None),
        interval_seconds=0.01,
    )
    assert monitor.capture_checkpoint()

    admitted = threading.Event()
    release = threading.Event()
    original_capture = monitor.capture_checkpoint
    periodic_results: list[bool] = []

    def pause_before_capture_lock() -> bool:
        admitted.set()
        assert release.wait(2)
        captured = original_capture()
        periodic_results.append(captured)
        return captured

    monkeypatch.setattr(monitor, "capture_checkpoint", pause_before_capture_lock)
    monitor.start()
    assert admitted.wait(1)
    (parent / "cpu.stat").write_text("usage_usec 1500000\n")
    finalized: list[bool] = []
    finalizer = threading.Thread(
        target=lambda: finalized.append(monitor.finalize_accounting())
    )
    finalizer.start()
    deadline = time.monotonic() + 1
    while (
        not any(sample.get("final_cumulative") for sample in monitor.samples)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert monitor.samples[-1].get("final_cumulative") is True

    release.set()
    finalizer.join(2)

    assert finalized == [True]
    assert periodic_results == [False]
    assert not finalizer.is_alive()
    assert [sample.get("final_cumulative", False) for sample in monitor.samples] == [
        False,
        True,
    ]
    assert monitor.summary(1)["authoritative"] is True


def test_finalization_waits_for_capture_already_holding_lock(
    tmp_path, monkeypatch
) -> None:
    monitor, _parent = _retained_cgroup_monitor(tmp_path)
    capture_holds_lock = threading.Event()
    release = threading.Event()
    original_capture = monitor._capture_cgroup_checkpoint

    def pause_while_holding_lock(*, final_cumulative: bool = False) -> bool:
        if not final_cumulative:
            capture_holds_lock.set()
            assert release.wait(2)
        return original_capture(final_cumulative=final_cumulative)

    monkeypatch.setattr(
        monitor, "_capture_cgroup_checkpoint", pause_while_holding_lock
    )
    sampler = threading.Thread(target=monitor.capture_checkpoint)
    sampler.start()
    assert capture_holds_lock.wait(1)
    finalized: list[bool] = []
    finalizer = threading.Thread(
        target=lambda: finalized.append(monitor.finalize_accounting())
    )
    finalizer.start()
    time.sleep(0.02)
    assert finalizer.is_alive()

    release.set()
    sampler.join(2)
    finalizer.join(2)

    assert finalized == [True]
    assert [sample.get("final_cumulative", False) for sample in monitor.samples] == [
        False,
        True,
    ]


def test_summary_rejects_trailing_or_duplicate_final_samples(tmp_path) -> None:
    monitor, _parent = _retained_cgroup_monitor(tmp_path)
    assert monitor.capture_checkpoint()
    assert monitor.finalize_accounting()
    ordinary = dict(monitor.samples[0])
    monitor.samples.append(ordinary)

    trailing = monitor.summary(1)
    assert trailing["accounting_availability"]["final_cumulative_cpu_sample"] is False
    assert trailing["authoritative"] is False

    monitor.samples[-1] = dict(monitor.samples[1])
    duplicate = monitor.summary(1)
    assert duplicate["accounting_availability"]["final_cumulative_cpu_sample"] is False
    assert duplicate["authoritative"] is False


def test_immediate_cgroup_disappearance_has_no_synthetic_final_sample(tmp_path) -> None:
    monitor = ResourceMonitor(
        SimpleNamespace(pid=1),
        cidfile=tmp_path / "missing.cid",
        proc_root=tmp_path / "proc",
        cgroup_root=tmp_path / "cgroup",
    )
    assert monitor.finalize_accounting() is False
    summary = monitor.summary(0)
    assert summary["over_time"] == []
    assert summary["accounting_availability"]["final_cumulative_cpu_sample"] is False
    assert summary["authoritative"] is False
