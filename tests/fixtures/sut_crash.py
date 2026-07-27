import os
import socket
import sys

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
sys.exit(7)
