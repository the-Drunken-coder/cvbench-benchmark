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
        if metadata.get("event") == "frame" and not sent:
            print("{malformed", flush=True)
            sent = True
