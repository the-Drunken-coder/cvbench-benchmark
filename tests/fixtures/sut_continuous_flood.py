import json
import os
import socket
import threading
from pathlib import Path

from cvbench.protocol import receive_message

Path(os.environ["CVBENCH_TEST_PID_FILE"]).write_text(str(os.getpid()))
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)


def consume_input() -> None:
    with sock, sock.makefile("rb") as stream:
        while True:
            metadata, _ = receive_message(stream)
            if metadata.get("event") == "benchmark_end":
                return


threading.Thread(target=consume_input, daemon=True).start()
record = {
    "schema_version": "cvbench.track/v1",
    "event": "track_update",
    "sequence_id": "flood",
    "source_timestamp_ns": 0,
    "track_id": "flood",
    "state": "confirmed",
    "support": "observed",
    "class_id": "synthetic_target",
    "confidence": 0.9,
    "geometry": {"type": "bbox_xyxy", "space": "source_pixels", "value": [1, 1, 10, 10]},
}
line = json.dumps(record)
while True:
    print(line, flush=True)
