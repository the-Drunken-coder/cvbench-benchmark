from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

RETENTION_CGROUP_NAME = "cvbench-retain"


def unavailable_resource_summary(runtime_seconds: float, runtime_type: str) -> dict[str, Any]:
    """Return a complete, explicitly non-authoritative summary for an unstarted runtime."""
    return {
        "sample_count": 0,
        "runtime_seconds": runtime_seconds,
        "average_cpu_percent": None,
        "peak_cpu_percent": None,
        "cpu_time_seconds": None,
        "cpu_seconds_per_native_source_second": None,
        "average_ram_bytes": None,
        "peak_ram_bytes": None,
        "gpu_available": False,
        "peak_vram_bytes": None,
        "gpu_accounting": {
            "available": False,
            "isolated": False,
            "authoritative": False,
            "note": "No isolated GPU device was assigned; host-wide GPU data is not reported.",
        },
        "disk_read_bytes": None,
        "disk_write_bytes": None,
        "network_rx_bytes": None,
        "network_tx_bytes": None,
        "peak_process_count": None,
        "peak_thread_count": None,
        "memory_growth_bytes": None,
        "by_scenario": {},
        "by_target_count": {},
        "by_phase": {},
        "cpu_time_by_phase_seconds": {},
        "fault_injection_samples": 0,
        "accounting_scope": (
            "container_cgroup_v2_external"
            if runtime_type == "docker"
            else "local_process_tree_best_effort"
        ),
        "accounting_availability": {
            "external_cgroup_v2": False,
            "final_cumulative_cpu_sample": False,
            "cpu_time": False,
            "cpu_percent": False,
            "peak_ram": False,
            "disk_io": False,
        },
        "authoritative": False,
        "over_time": [],
    }


def parse_size(value: str) -> float:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgGtT]?i?[bB])?\s*", value)
    if not match:
        raise ValueError(f"invalid size: {value}")
    number = float(match.group(1))
    unit = (match.group(2) or "B").lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    return number * factors[unit]


def _pair_sizes(value: str) -> tuple[float, float]:
    left, _, right = value.partition("/")
    return parse_size(left.strip()), parse_size(right.strip())


def parse_docker_stats(payload: str | dict[str, Any]) -> dict[str, float | int | None]:
    data = json.loads(payload) if isinstance(payload, str) else payload
    memory, memory_limit = _pair_sizes(str(data.get("MemUsage", "0B / 0B")))
    network_rx, network_tx = _pair_sizes(str(data.get("NetIO", "0B / 0B")))
    disk_read, disk_write = _pair_sizes(str(data.get("BlockIO", "0B / 0B")))
    return {
        "cpu_percent": float(str(data.get("CPUPerc", "0")).rstrip("%")),
        "memory_bytes": memory,
        "memory_limit_bytes": memory_limit,
        "disk_read_bytes": disk_read,
        "disk_write_bytes": disk_write,
        "network_rx_bytes": network_rx,
        "network_tx_bytes": network_tx,
        "process_count": int(data.get("PIDs", 0) or 0),
        "thread_count": None,
    }


def parse_cpu_stat(payload: str) -> float | None:
    for line in payload.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            return int(value) / 1_000_000
        if key == "usage_nsec":
            return int(value) / 1_000_000_000
    return None


def parse_io_stat(payload: str) -> tuple[int, int]:
    read_bytes = 0
    write_bytes = 0
    for line in payload.splitlines():
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key == "rbytes":
                read_bytes += int(value)
            elif key == "wbytes":
                write_bytes += int(value)
    return read_bytes, write_bytes


def cgroup_v2_path(payload: str, cgroup_root: Path) -> Path | None:
    for line in payload.splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers:
            return cgroup_root / path.lstrip("/")
    return None


class ResourceMonitor:
    def __init__(
        self,
        process: subprocess.Popen[str],
        interval_seconds: float = 0.1,
        cidfile: Path | None = None,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        cgroup_parent_name: str | None = None,
        configured_cgroup_path: Path | None = None,
    ):
        self.process = process
        self.interval_seconds = interval_seconds
        self.cidfile = cidfile
        self.proc_root = proc_root
        self.cgroup_root = cgroup_root
        self.cgroup_parent_name = cgroup_parent_name
        self.configured_cgroup_path = configured_cgroup_path
        self.samples: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {
            "phase": "startup",
            "scenario": None,
            "target_count": None,
            "fault_injection": False,
        }
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started_ns = time.monotonic_ns()
        self._lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self._container_id: str | None = None
        self._cgroup_path: Path | None = None
        self._retention_cgroup_path: Path | None = None
        self._last_cpu: tuple[int, float] | None = None
        self._finalization_started = False
        self._final_sample_complete = False

    @property
    def accounting_cgroup_path(self) -> Path | None:
        return self._cgroup_path

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(3)

    def set_context(
        self,
        scenario: str | None,
        target_count: int | None,
        fault_injection: bool,
        *,
        phase: str = "delivery",
    ) -> None:
        self.context = {
            "phase": phase,
            "scenario": scenario,
            "target_count": target_count,
            "fault_injection": fault_injection,
        }

    def _run(self) -> None:
        if self.cidfile is not None:
            self._run_docker()
        else:
            self._run_process()

    def _run_process(self) -> None:
        try:
            root = psutil.Process(self.process.pid)
            root.cpu_percent(None)
        except psutil.Error:
            return
        while not self._stop.wait(self.interval_seconds):
            try:
                processes = [root, *root.children(recursive=True)]
                cpu = memory = read_bytes = write_bytes = cpu_time = 0.0
                threads = 0
                alive = 0
                for process in processes:
                    try:
                        cpu += process.cpu_percent(None)
                        memory += process.memory_info().rss
                        if hasattr(process, "io_counters"):
                            io = process.io_counters()
                            read_bytes += io.read_bytes
                            write_bytes += io.write_bytes
                        times = process.cpu_times()
                        cpu_time += times.user + times.system
                        threads += process.num_threads()
                        alive += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                self.samples.append(
                    {
                        "elapsed_ms": (time.monotonic_ns() - self._started_ns) / 1_000_000,
                        "cpu_percent": cpu,
                        "cpu_time_seconds": cpu_time,
                        "memory_bytes": memory,
                        "disk_read_bytes": read_bytes,
                        "disk_write_bytes": write_bytes,
                        "network_rx_bytes": None,
                        "network_tx_bytes": None,
                        "process_count": alive,
                        "thread_count": threads,
                        "gpu_percent": None,
                        "vram_bytes": None,
                        **self.context,
                    }
                )
            except psutil.Error:
                break

    def _run_docker(self) -> None:
        deadline = time.monotonic() + 10
        container_id = ""
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.cidfile.exists():
                container_id = self.cidfile.read_text().strip()
                if container_id:
                    break
            self._stop.wait(0.05)
        if not container_id:
            return
        self._container_id = container_id
        while not self._stop.is_set() and not self.capture_checkpoint():
            self._stop.wait(0.01)
        while not self._stop.wait(self.interval_seconds):
            if not self.capture_checkpoint() and self.process.poll() is not None:
                break

    def _resolve_cgroup(self) -> Path | None:
        if self._cgroup_path is not None:
            return self._cgroup_path
        if (
            self.configured_cgroup_path is not None
            and self.cgroup_parent_name is not None
            and self.configured_cgroup_path.name == self.cgroup_parent_name
            and (self.configured_cgroup_path / "cpu.stat").is_file()
        ):
            return self._retain_cgroup(self.configured_cgroup_path)
        if not self._container_id:
            return None
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Pid}}", self._container_id],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            return None
        try:
            pid = int(result.stdout.strip())
            payload = (self.proc_root / str(pid) / "cgroup").read_text()
        except (OSError, ValueError):
            return None
        path = cgroup_v2_path(payload, self.cgroup_root)
        if path is None or not (path / "cpu.stat").is_file():
            return None
        if self.cgroup_parent_name is not None:
            path = path.parent
            if path.name != self.cgroup_parent_name or not (path / "cpu.stat").is_file():
                return None
        return self._retain_cgroup(path)

    def _retain_cgroup(self, path: Path) -> Path | None:
        retention = path / RETENTION_CGROUP_NAME
        try:
            retention.mkdir()
        except FileExistsError:
            pass
        except OSError:
            if not shutil.which("sudo"):
                return None
            try:
                result = subprocess.run(
                    ["sudo", "-n", "mkdir", "--", str(retention)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode != 0 and not retention.is_dir():
                return None
        if not retention.is_dir():
            return None
        self._retention_cgroup_path = retention
        self._cgroup_path = path
        return path

    @staticmethod
    def _read_number(path: Path) -> int | None:
        try:
            value = path.read_text().strip()
            return None if value == "max" else int(value)
        except (OSError, ValueError):
            return None

    def capture_checkpoint(self) -> bool:
        """Capture externally from the host cgroup; never execute in the image."""
        with self._capture_lock:
            if self._finalization_started:
                return False
            return self._capture_cgroup_checkpoint()

    def _capture_cgroup_checkpoint(self, *, final_cumulative: bool = False) -> bool:
        if self.cidfile is None:
            return False
        if self._container_id is None and self.cidfile.exists():
            try:
                self._container_id = self.cidfile.read_text().strip() or None
            except OSError:
                return False
        path = self._resolve_cgroup()
        if path is None:
            return False
        try:
            cpu_time = parse_cpu_stat((path / "cpu.stat").read_text())
            disk_read, disk_write = parse_io_stat((path / "io.stat").read_text())
        except (OSError, ValueError):
            return False
        if cpu_time is None:
            return False
        sampled_ns = time.monotonic_ns()
        cpu_percent = None
        if self._last_cpu is not None:
            previous_ns, previous_cpu = self._last_cpu
            elapsed = (sampled_ns - previous_ns) / 1_000_000_000
            if elapsed > 0:
                cpu_percent = max(0.0, (cpu_time - previous_cpu) / elapsed * 100)
        self._last_cpu = (sampled_ns, cpu_time)
        sample = {
            "elapsed_ms": (sampled_ns - self._started_ns) / 1_000_000,
            "cpu_percent": cpu_percent,
            "cpu_time_seconds": cpu_time,
            "memory_bytes": self._read_number(path / "memory.current"),
            "memory_limit_bytes": self._read_number(path / "memory.max"),
            "memory_peak_bytes": self._read_number(path / "memory.peak"),
            "disk_read_bytes": disk_read,
            "disk_write_bytes": disk_write,
            "network_rx_bytes": None,
            "network_tx_bytes": None,
            "process_count": self._read_number(path / "pids.current"),
            "thread_count": None,
            "gpu_percent": None,
            "vram_bytes": None,
            "accounting_source": "host_cgroup_v2",
            **self.context,
        }
        if final_cumulative:
            sample["final_cumulative"] = True
        with self._lock:
            self.samples.append(sample)
        return True

    @staticmethod
    def _has_one_terminal_final_sample(samples: list[dict[str, Any]]) -> bool:
        final_indexes = [
            index
            for index, sample in enumerate(samples)
            if sample.get("final_cumulative") is True
        ]
        return final_indexes == [len(samples) - 1]

    def finalize_accounting(self) -> bool:
        """Capture and certify a new cumulative sample at the scoring boundary."""
        self._stop.set()
        with self._capture_lock:
            if not self._finalization_started:
                self._finalization_started = True
                self._final_sample_complete = self._capture_cgroup_checkpoint(
                    final_cumulative=True
                )
            with self._lock:
                captured = (
                    self._final_sample_complete
                    and self._has_one_terminal_final_sample(self.samples)
                )
        if self._thread.is_alive():
            self._thread.join(3)
        return captured

    def add_gpu_snapshot(self) -> None:
        if not shutil.which("nvidia-smi"):
            return
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            return
        try:
            rows = [[float(value.strip()) for value in line.split(",")] for line in result.stdout.splitlines()]
            gpu = sum(row[0] for row in rows) / len(rows)
            vram = sum(row[1] for row in rows) * 1024 * 1024
        except (ValueError, IndexError):
            return
        if self.samples:
            self.samples[-1]["gpu_percent"] = gpu
            self.samples[-1]["vram_bytes"] = vram

    def summary(self, runtime_seconds: float) -> dict[str, Any]:
        with self._lock:
            samples = [dict(sample) for sample in self.samples]
        memory = [float(sample["memory_bytes"]) for sample in samples if sample.get("memory_bytes") is not None]
        cpu = [float(sample["cpu_percent"]) for sample in samples if sample.get("cpu_percent") is not None]
        vram = [float(sample["vram_bytes"]) for sample in samples if sample.get("vram_bytes") is not None]
        grouped_scenario: dict[str, list[dict[str, Any]]] = {}
        grouped_targets: dict[str, list[dict[str, Any]]] = {}
        grouped_phase: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            grouped_scenario.setdefault(str(sample.get("scenario") or "startup"), []).append(sample)
            grouped_targets.setdefault(str(sample.get("target_count") or 0), []).append(sample)
            grouped_phase.setdefault(str(sample.get("phase") or "startup"), []).append(sample)

        def grouped_summary(groups: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float | int | None]]:
            result = {}
            for key, rows in sorted(groups.items()):
                cpu_times = [
                    float(row["cpu_time_seconds"])
                    for row in rows
                    if row.get("cpu_time_seconds") is not None
                ]
                result[key] = {
                    "sample_count": len(rows),
                    "average_cpu_percent": sum(float(row.get("cpu_percent") or 0) for row in rows) / len(rows),
                    "peak_ram_bytes": max((float(row.get("memory_bytes") or 0) for row in rows), default=None),
                    "cpu_time_delta_seconds": (
                        max(cpu_times) - min(cpu_times) if len(cpu_times) >= 2 else None
                    ),
                }
            return result

        phase_summary = grouped_summary(grouped_phase)
        cpu_time_seconds = max(
            (sample["cpu_time_seconds"] for sample in samples if sample.get("cpu_time_seconds") is not None),
            default=None,
        )
        disk_read_bytes = max(
            (sample.get("disk_read_bytes") for sample in samples if sample.get("disk_read_bytes") is not None),
            default=None,
        )
        disk_write_bytes = max(
            (sample.get("disk_write_bytes") for sample in samples if sample.get("disk_write_bytes") is not None),
            default=None,
        )
        external_cgroup = any(
            sample.get("accounting_source") == "host_cgroup_v2" for sample in samples
        )
        final_sample_available = self._has_one_terminal_final_sample(samples)
        availability = {
            "external_cgroup_v2": external_cgroup,
            "final_cumulative_cpu_sample": final_sample_available,
            "cpu_time": cpu_time_seconds is not None,
            "cpu_percent": bool(cpu),
            "peak_ram": bool(memory),
            "disk_io": disk_read_bytes is not None and disk_write_bytes is not None,
        }
        return {
            "sample_count": len(samples),
            "runtime_seconds": runtime_seconds,
            "average_cpu_percent": sum(cpu) / len(cpu) if cpu else None,
            "peak_cpu_percent": max(cpu) if cpu else None,
            "cpu_time_seconds": cpu_time_seconds,
            "average_ram_bytes": sum(memory) / len(memory) if memory else None,
            "peak_ram_bytes": max(
                [*memory, *[
                    float(sample["memory_peak_bytes"])
                    for sample in samples
                    if sample.get("memory_peak_bytes") is not None
                ]],
                default=None,
            ),
            "gpu_available": bool(vram),
            "peak_vram_bytes": max(vram) if vram else None,
            "disk_read_bytes": disk_read_bytes,
            "disk_write_bytes": disk_write_bytes,
            "network_rx_bytes": max((sample.get("network_rx_bytes") or 0 for sample in samples), default=None),
            "network_tx_bytes": max((sample.get("network_tx_bytes") or 0 for sample in samples), default=None),
            "peak_process_count": max((sample.get("process_count") or 0 for sample in samples), default=None),
            "peak_thread_count": max((sample.get("thread_count") or 0 for sample in samples), default=None),
            "memory_growth_bytes": memory[-1] - memory[0] if len(memory) >= 2 else None,
            "by_scenario": grouped_summary(grouped_scenario),
            "by_target_count": grouped_summary(grouped_targets),
            "by_phase": phase_summary,
            "cpu_time_by_phase_seconds": {
                phase: summary["cpu_time_delta_seconds"]
                for phase, summary in phase_summary.items()
            },
            "fault_injection_samples": sum(bool(sample.get("fault_injection")) for sample in samples),
            "accounting_scope": (
                "container_cgroup_v2_external"
                if self.cidfile is not None
                else "local_process_tree_best_effort"
            ),
            "accounting_availability": availability,
            "authoritative": all(availability.values()),
            "over_time": samples,
        }

    def write_csv(self, path: Path) -> None:
        fields = [
            "elapsed_ms",
            "cpu_percent",
            "cpu_time_seconds",
            "memory_bytes",
            "disk_read_bytes",
            "disk_write_bytes",
            "network_rx_bytes",
            "network_tx_bytes",
            "process_count",
            "thread_count",
            "gpu_percent",
            "vram_bytes",
            "phase",
            "scenario",
            "target_count",
            "fault_injection",
        ]
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.samples)
