import json
import os
import socket

from cvbench.protocol import receive_message

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
sent = False
with sock, sock.makefile("rb") as stream:
    while True:
        metadata, _ = receive_message(stream)
        if metadata.get("event") == "benchmark_end":
            break
        if metadata.get("event") != "frame" or sent:
            continue
        print(
            json.dumps(
                {
                    "schema_version": "cvbench.track/v1",
                    "event": "track_update",
                    "sequence_id": metadata["sequence_id"],
                    "source_timestamp_ns": metadata["source_timestamp_ns"] + 10**15,
                    "track_id": "spoof",
                    "state": "confirmed",
                    "support": "observed",
                    "class_id": "synthetic_target",
                    "confidence": 0.9,
                    "geometry": {
                        "type": "bbox_xyxy",
                        "space": "source_pixels",
                        "value": [1, 1, 10, 10],
                    },
                }
            ),
            flush=True,
        )
        sent = True
