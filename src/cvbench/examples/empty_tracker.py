from __future__ import annotations

import os
import socket
import sys

from cvbench.protocol import receive_message


def main() -> int:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.environ.get("CVBENCH_INPUT_SOCKET", "/run/cvbench/input.sock"))
    print("CVBENCH_READY", flush=True)
    with sock, sock.makefile("rb") as stream:
        while True:
            metadata, _payload = receive_message(stream)
            if metadata.get("event") == "benchmark_end":
                sock.shutdown(socket.SHUT_WR)
                return 0


if __name__ == "__main__":
    sys.exit(main())
