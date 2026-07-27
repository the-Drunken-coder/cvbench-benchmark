import json
import os
import socket
import sys
import time
from pathlib import Path

from cvbench.protocol import receive_message

pid_file = os.environ.get("CVBENCH_TEST_PID_FILE")
if pid_file:
    Path(pid_file).write_text(str(os.getpid()))


def _record(frame: dict, track_id: str) -> str:
    return json.dumps(
        {
            "schema_version": "cvbench.track/v1",
            "event": "track_update",
            "sequence_id": frame["sequence_id"],
            "source_timestamp_ns": frame["source_timestamp_ns"],
            "track_id": track_id,
            "state": "confirmed",
            "support": "observed",
            "class_id": "synthetic_target",
            "confidence": 0.9,
            "geometry": {
                "type": "bbox_xyxy",
                "space": "source_pixels",
                "value": [1, 1, 10, 10],
            },
        },
        separators=(",", ":"),
    )


sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
first_frame = None
mode = os.environ.get("CVBENCH_HALF_CLOSE_MODE", "clean")
with sock, sock.makefile("rb") as stream:
    while True:
        metadata, _ = receive_message(stream)
        if metadata.get("event") == "frame" and first_frame is None:
            first_frame = metadata
        if metadata.get("event") != "benchmark_end":
            continue
        assert first_frame is not None
        lines = [_record(first_frame, "buffered-a")]
        if mode in {"malformed-before", "malformed-immediate-clean-exit"}:
            lines.append("{malformed-before-boundary")
        lines.append(_record(first_frame, "buffered-b"))
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        sock.shutdown(socket.SHUT_WR)
        if mode in {"immediate-clean-exit", "malformed-immediate-clean-exit"}:
            break
        time.sleep(0.12)
        sys.stdout.write(_record(first_frame, "late") + "\n{malformed-late\n")
        sys.stdout.flush()
        while stream.read(1):
            pass
        break
