import os
import socket
import time
from pathlib import Path

from cvbench.protocol import receive_message

Path(os.environ["CVBENCH_TEST_PID_FILE"]).write_text(str(os.getpid()))

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
with sock, sock.makefile("rb") as stream:
    while True:
        metadata, _ = receive_message(stream)
        if metadata.get("event") == "benchmark_end":
            time.sleep(10)
