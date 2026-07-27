import os
import socket
import sys
import time

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
print("CVBENCH_READY", flush=True)
sys.stdout.write("x" * 8192)
sys.stdout.flush()
time.sleep(10)
