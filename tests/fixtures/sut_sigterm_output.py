import json
import os
import signal
import socket
import time
from pathlib import Path

from cvbench.protocol import receive_message

Path(os.environ["CVBENCH_TEST_PID_FILE"]).write_text(str(os.getpid()))
last_frame = {}


def emit_after_deadline(_signum, _frame):
    if last_frame:
        print(
            json.dumps(
                {
                    "schema_version": "cvbench.track/v1",
                    "event": "track_update",
                    "sequence_id": last_frame["sequence_id"],
                    "source_timestamp_ns": last_frame["source_timestamp_ns"],
                    "track_id": "too-late",
                    "state": "confirmed",
                    "support": "observed",
                    "class_id": "synthetic_target",
                    "confidence": 1,
                    "geometry": {
                        "type": "bbox_xyxy",
                        "space": "source_pixels",
                        "value": [10, 10, 20, 20],
                    },
                }
            ),
            flush=True,
        )


signal.signal(signal.SIGTERM, emit_after_deadline)
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
with sock, sock.makefile("rb") as stream:
    while True:
        metadata, _ = receive_message(stream)
        if metadata.get("event") == "frame":
            last_frame = metadata
        if metadata.get("event") == "benchmark_end":
            time.sleep(10)
