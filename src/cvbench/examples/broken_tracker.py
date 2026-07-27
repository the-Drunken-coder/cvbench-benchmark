from __future__ import annotations

import json
import os
import socket
import sys
import time

from cvbench.protocol import receive_message


def main() -> int:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.environ.get("CVBENCH_INPUT_SOCKET", "/run/cvbench/input.sock"))
    print("CVBENCH_READY", flush=True)
    counter = 0
    with sock, sock.makefile("rb") as stream:
        while True:
            metadata, _ = receive_message(stream)
            if metadata.get("event") == "benchmark_end":
                break
            if metadata.get("event") != "frame":
                continue
            counter += 1
            time.sleep(0.03)
            print(
                json.dumps(
                    {
                        "schema_version": "cvbench.track/v1",
                        "event": "track_started",
                        "sequence_id": metadata["sequence_id"],
                        "source_timestamp_ns": metadata["source_timestamp_ns"],
                        "track_id": f"broken-{counter}",
                        "state": "confirmed",
                        "support": "observed",
                        "class_id": "synthetic_target",
                        "confidence": 0.99,
                        "geometry": {"type": "bbox_xyxy", "space": "source_pixels", "value": [3, 3, 24, 29]},
                    }
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
