"""A real offline child for scientific-channel lifecycle fault witnesses."""

import argparse
import errno
import os
from pathlib import Path
import signal
import socket
import struct
import sys
from time import sleep

# The API test image keeps the provider under /opt; isolated Python ignores PYTHONPATH.
if Path("/opt/secs_inference/provider/worker_protocol.py").is_file():
    sys.path.insert(0, "/opt")
from secs_inference.provider.worker_protocol import serve_scientific_session


parser = argparse.ArgumentParser()
parser.add_argument("--session-fd", type=int, required=True)
parser.add_argument("--lease-fd", type=int, required=True)
parser.add_argument("--operation", required=True)
parser.add_argument("--mode", required=True)
parser.add_argument("--pid-file", required=True)
parser.add_argument("--config-file")
options = parser.parse_args()
os.fstat(options.lease_fd)
Path(options.pid_file).write_text(str(os.getpid()))
config = Path(options.config_file).read_text() if options.config_file else None
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
    if options.mode == "startup_failure":
        raise RuntimeError("private fixture startup failure")
    calls = 0
    def handle(command):
        nonlocal calls
        calls += 1
        if command["operation"] == "prepare" and options.mode == "unconfirmed_execute":
            Path(options.pid_file + ".prepare_entered").touch()
            return {"outcome": "prepared", "prepared_handle": 7}
        if command["operation"] == "execute" and options.mode == "unconfirmed_execute":
            Path(options.pid_file + ".entered").touch()
            while True:
                sleep(1)
        if command["operation"] == "execute" and options.mode == "malformed_execute":
            return {"outcome": "failed"}
        if command["operation"] == "execute" and options.mode == "invalid_json_execute":
            connection.sendall(struct.pack("!I", 1) + b"{")
            while True:
                sleep(1)
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
        if command["operation"] == "config":
            return {"outcome": "observed", "pid": os.getpid(), "config": config}
        return {"outcome": "observed", "pid": os.getpid(), "calls": calls}
    return handle


serve_scientific_session(connection, load)
