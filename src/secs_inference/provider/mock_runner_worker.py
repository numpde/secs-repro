"""Serve a bounded no-computation runner for deployment checks."""

import argparse
import os
import socket

from secs_inference.provider.worker_protocol import serve_scientific_session


def load_mock_runner():
    pending = None
    def handle(command):
        nonlocal pending
        if command.get("operation") == "prepare" and pending is None:
            pending = command["attempt_ref"]
            return {"outcome": "prepared", "prepared_handle": 1}
        if (command.get("operation") == "execute" and pending == command.get("attempt_ref")
                and command.get("prepared_handle") == 1):
            pending = None
            return {"outcome": "mock", "message": "No model or scientific analysis was run."}
        raise ValueError("Mock runner requires one matching prepared Attempt")
    return handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--operation", required=True)
    options = parser.parse_args()
    if options.session_fd < 0 or options.lease_fd < 0 or options.session_fd == options.lease_fd:
        raise ValueError("Mock runner session and lifetime lease require distinct open descriptors")
    os.fstat(options.lease_fd)
    serve_scientific_session(socket.socket(fileno=options.session_fd), load_mock_runner)


if __name__ == "__main__":
    main()
