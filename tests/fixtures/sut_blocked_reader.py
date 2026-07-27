import os
import socket
import time

from cvbench.protocol import receive_message

with open(os.environ["CVBENCH_TEST_PID_FILE"], "w") as handle:
    handle.write(str(os.getpid()))

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
with sock, sock.makefile("rb") as stream:
    metadata, _ = receive_message(stream)
    assert metadata["event"] == "stream_start"
    time.sleep(30)
