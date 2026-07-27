from __future__ import annotations

import array
import fcntl
import json
import os
import select
import termios
import threading
import time
from collections import deque
from subprocess import Popen
from typing import Any

from .errors import ProtocolError
from .model import CollectedRecord
from .protocol import validate_track_record


class OutputCollector:
    def __init__(
        self,
        process: Popen[str],
        readiness_pattern: str,
        max_records: int,
        max_line_bytes: int,
        max_total_bytes: int,
        max_records_per_second: int,
        out_of_bounds: str,
    ):
        self.process = process
        self.readiness_pattern = readiness_pattern
        self.max_records = max_records
        self.max_line_bytes = max_line_bytes
        self.max_total_bytes = max_total_bytes
        self.max_records_per_second = max_records_per_second
        self.out_of_bounds = out_of_bounds
        self.records: list[CollectedRecord] = []
        self.errors: list[str] = []
        self.invalid_record_count = 0
        self.stderr: list[str] = []
        self.ready = threading.Event()
        self.flooded = threading.Event()
        self.scoring_closed = threading.Event()
        self.stdout_closed = threading.Event()
        self.output_boundary_requested = threading.Event()
        self.output_boundary_drained = threading.Event()
        self.limit_reason: str | None = None
        self.first_output_timestamp_ns: int | None = None
        self._lock = threading.Lock()
        self._stdout_state_lock = threading.Lock()
        self._stdout_fd: int | None = None
        self._stdout_read_bytes = 0
        self._stdout_processed_bytes = 0
        self._output_boundary_target: int | None = None
        self._frames: dict[tuple[str, int], tuple[str, tuple[int, int]]] = {}
        self._pending: dict[tuple[str, int], list[tuple[int, Any, str]]] = {}
        self._output_record_count = 0
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)

    def start(self) -> None:
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        file_descriptor = self.process.stdout.fileno()
        os.set_blocking(file_descriptor, False)
        with self._stdout_state_lock:
            self._stdout_fd = file_descriptor
        buffer = bytearray()
        total_bytes = 0
        recent_records: deque[int] = deque()
        boundary_acknowledged = False
        try:
            while not self.flooded.is_set():
                if (
                    self.output_boundary_requested.is_set()
                    and self._output_boundary_target is None
                ):
                    self._snapshot_output_boundary()
                with self._stdout_state_lock:
                    boundary_target = self._output_boundary_target
                    boundary_ready = (
                        boundary_target is not None
                        and self._stdout_processed_bytes >= boundary_target
                    )
                if boundary_ready and not boundary_acknowledged:
                    if buffer:
                        self._consume_line(bytes(buffer), recent_records)
                        buffer.clear()
                    self.close_scoring()
                    self.output_boundary_drained.set()
                    boundary_acknowledged = True
                readable, _, _ = select.select([file_descriptor], [], [], 0.02)
                if not readable:
                    continue
                with self._stdout_state_lock:
                    read_limit = 65536
                    if self._output_boundary_target is not None and not boundary_acknowledged:
                        remaining = self._output_boundary_target - self._stdout_read_bytes
                        if remaining <= 0:
                            continue
                        read_limit = min(read_limit, remaining)
                    try:
                        chunk = os.read(file_descriptor, read_limit)
                    except BlockingIOError:
                        continue
                    self._stdout_read_bytes += len(chunk)
                if not chunk:
                    if buffer:
                        self._consume_line(bytes(buffer), recent_records)
                    return
                total_bytes += len(chunk)
                if total_bytes > self.max_total_bytes:
                    self._set_limit(f"total stdout byte limit exceeded ({self.max_total_bytes})")
                    return
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if len(raw_line) > self.max_line_bytes:
                        self._set_limit(f"stdout line byte limit exceeded ({self.max_line_bytes})")
                        return
                    self._consume_line(raw_line, recent_records)
                    if self.flooded.is_set():
                        return
                if len(buffer) > self.max_line_bytes:
                    self._set_limit(f"stdout line byte limit exceeded ({self.max_line_bytes})")
                    return
                with self._stdout_state_lock:
                    self._stdout_processed_bytes += len(chunk)
        finally:
            self.stdout_closed.set()

    def _consume_line(self, raw_line: bytes, recent_records: deque[int]) -> None:
        received = time.monotonic_ns()
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            self._record_invalid(f"output is not UTF-8: {exc}", raw_line[:300].decode("utf-8", "replace"))
            return
        if not line:
            return
        if not self.ready.is_set() and self.readiness_pattern in line:
            self.ready.set()
            return
        with self._lock:
            if self.scoring_closed.is_set():
                return
            self._output_record_count += 1
            record_number = self._output_record_count
        if record_number > self.max_records:
            self._set_limit(f"output record limit exceeded ({self.max_records})")
            return
        cutoff = received - 1_000_000_000
        while recent_records and recent_records[0] <= cutoff:
            recent_records.popleft()
        recent_records.append(received)
        if len(recent_records) > self.max_records_per_second:
            self._set_limit(f"output rate limit exceeded ({self.max_records_per_second} records/second)")
            return
        try:
            raw: Any = json.loads(line)
        except (json.JSONDecodeError, ProtocolError) as exc:
            self._record_invalid(str(exc), line[:300])
            return
        if not isinstance(raw, dict):
            self._record_invalid("output record must be an object", line[:300])
            return
        frame_key = (raw.get("sequence_id"), raw.get("source_timestamp_ns"))
        with self._lock:
            state = self._frames.get(frame_key)
            if state is not None and state[0] == "pending":
                self._pending.setdefault(frame_key, []).append((received, raw, line[:300]))
                return
        self._validate_released_record(frame_key, received, raw, line[:300])

    def begin_frame(self, frame_key: tuple[str, int], size: tuple[int, int]) -> None:
        """Register an in-flight frame without authorizing output for it."""
        with self._lock:
            self._frames[frame_key] = ("pending", size)

    def finish_frame(self, frame_key: tuple[str, int], *, delivered: bool) -> None:
        """Authorize queued outputs only after the complete frame send succeeds."""
        with self._lock:
            state = self._frames.get(frame_key)
            pending = self._pending.pop(frame_key, [])
            if state is None:
                return
            size = state[1]
            if delivered:
                self._frames[frame_key] = ("released", size)
            else:
                self._frames.pop(frame_key, None)
        if delivered:
            for received, raw, sample in pending:
                self._validate_released_record(frame_key, received, raw, sample)
        else:
            for _received, _raw, sample in pending:
                self._record_invalid(
                    "source_timestamp_ns identifies a frame whose delivery failed",
                    sample,
                )

    def _validate_released_record(
        self,
        frame_key: tuple[Any, Any],
        received: int,
        raw: Any,
        sample: str,
    ) -> None:
        with self._lock:
            state = self._frames.get(frame_key)
        if state is None or state[0] != "released":
            self._record_invalid(
                "source_timestamp_ns does not identify a successfully delivered frame",
                sample,
            )
            return
        try:
            record = validate_track_record(
                raw,
                frame_size=state[1],
                out_of_bounds=self.out_of_bounds,
            )
        except ProtocolError as exc:
            self._record_invalid(str(exc), sample)
            return
        with self._lock:
            if self.scoring_closed.is_set():
                return
            if self.first_output_timestamp_ns is None:
                self.first_output_timestamp_ns = received
            self.records.append(CollectedRecord(received, record))

    def _record_invalid(self, error: str, sample: str) -> None:
        with self._lock:
            self.invalid_record_count += 1
            if len(self.errors) < 1000:
                self.errors.append(f"malformed output: {error}; line={sample}")

    def _set_limit(self, reason: str) -> None:
        with self._lock:
            if self.flooded.is_set():
                return
            self.limit_reason = reason
            self.errors.append(f"output limit exceeded: {reason}")
            self.flooded.set()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        file_descriptor = self.process.stderr.fileno()
        buffer = bytearray()
        while chunk := os.read(file_descriptor, 65536):
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[: min(newline, 4096)]).decode("utf-8", "replace")
                del buffer[: newline + 1]
                with self._lock:
                    if len(self.stderr) < 1000:
                        self.stderr.append(line)
            if len(buffer) > 4096:
                del buffer[4096:]

    def snapshot(self) -> tuple[list[CollectedRecord], list[str], list[str]]:
        with self._lock:
            return list(self.records), list(self.errors), list(self.stderr)

    def close_scoring(self) -> None:
        with self._lock:
            self.scoring_closed.set()

    def request_output_boundary(self) -> bool:
        """Drain stdout already written before a socket half-close, then close scoring."""
        self.output_boundary_requested.set()
        self._snapshot_output_boundary()
        return self.output_boundary_drained.is_set()

    def _snapshot_output_boundary(self) -> None:
        with self._stdout_state_lock:
            if self._output_boundary_target is not None or self._stdout_fd is None:
                return
            pending = array.array("i", [0])
            try:
                fcntl.ioctl(self._stdout_fd, termios.FIONREAD, pending, True)
            except OSError as exc:
                self._set_limit(f"stdout completion boundary snapshot failed: {exc}")
                return
            self._output_boundary_target = self._stdout_read_bytes + max(0, pending[0])

    def join(self, timeout: float = 2.0) -> None:
        self._stdout_thread.join(timeout)
        self._stderr_thread.join(timeout)
