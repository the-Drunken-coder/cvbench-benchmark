import json
import os
import socket
import time

from cvbench.protocol import receive_message

delay_ms = float(os.environ.get("CVBENCH_TEST_OUTPUT_DELAY_MS", "0"))
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
with sock, sock.makefile("rb") as stream:
    while True:
        metadata, _ = receive_message(stream)
        if metadata.get("event") == "benchmark_end":
            break
        if metadata.get("event") != "frame":
            continue
        if delay_ms:
            time.sleep(delay_ms / 1000)
        print(
            json.dumps(
                {
                    "schema_version": "cvbench.track/v1",
                    "event": "track_update",
                    "sequence_id": metadata["sequence_id"],
                    "source_timestamp_ns": metadata["source_timestamp_ns"],
                    "track_id": "immediate",
                    "state": "confirmed",
                    "support": "observed",
                    "class_id": "synthetic_target",
                    "confidence": 0.9,
                    "geometry": {
                        "type": "bbox_xyxy",
                        "space": "source_pixels",
                        "value": [1, 1, 9, 9],
                    },
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
