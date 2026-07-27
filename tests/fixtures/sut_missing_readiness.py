import os
import socket
import time
from pathlib import Path

Path(os.environ["CVBENCH_TEST_PID_FILE"]).write_text(str(os.getpid()))

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["CVBENCH_INPUT_SOCKET"])
time.sleep(10)
