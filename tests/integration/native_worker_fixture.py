"""A real offline child for scientific-channel lifecycle fault witnesses."""

import argparse
import errno
import os
from pathlib import Path
import signal
import socket
from time import sleep

from secs_inference.provider.worker_protocol import serve_scientific_session


parser = argparse.ArgumentParser()
parser.add_argument("--session-fd", type=int, required=True)
parser.add_argument("--lease-fd", type=int, required=True)
parser.add_argument("--operation", required=True)
parser.add_argument("--mode", required=True)
parser.add_argument("--pid-file", required=True)
options = parser.parse_args()
os.fstat(options.lease_fd)
Path(options.pid_file).write_text(str(os.getpid()))
connection = socket.socket(fileno=options.session_fd)
if options.mode == "startup_exit":
    while not Path(options.pid_file + ".exit").exists():
        sleep(0.01)
    os._exit(23)
if options.mode == "startup_eof_alive":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.shutdown(socket.SHUT_RDWR)
    connection.close()
    while True:
        sleep(1)


def load():
    calls = 0
    def handle(command):
        nonlocal calls
        calls += 1
        if command["operation"] == "hang":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(command["entered"]).touch()
            while True:
                sleep(1)
        if command["operation"] == "exit":
            os._exit(23)
        if command["operation"] == "fail":
            try:
                raise OSError(errno.EIO, "private fixture path")
            except OSError as cause:
                raise RuntimeError("private fixture source content") from cause
        return {"outcome": "observed", "pid": os.getpid(), "calls": calls}
    return handle


serve_scientific_session(connection, load)
