"""Capture the worker configuration once for the native supervisor lifetime."""

import os
from pathlib import Path
import tomllib

from provider_core.startup_file import read_startup_file


SOURCE = Path("/run/config/worker/worker.toml")
SNAPSHOT = Path("/tmp/native-config/worker.toml")


def capture(source, destination, expected_device, expected_mock_index=False, expected_mock_runner=False):
    raw = read_startup_file(source, maximum_bytes=65536, label="Scientific worker configuration")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Cannot capture scientific worker configuration: invalid TOML.") from error
    device, mock_index = document.get("device", "cuda:0"), document.get("mock_index", False)
    mock_runner = document.get("mock_runner", False)
    if mock_index is True and set(document) != {"device", "mock_index"}:
        raise ValueError("Cannot start mock index worker: its search settings are fixed.")
    if mock_runner is True and set(document) != {"device", "mock_runner"}:
        raise ValueError("Cannot start mock runner: its configuration has unexpected fields.")
    if (expected_device not in ("cpu", "cuda:0") or device != expected_device
            or type(mock_index) is not bool or mock_index is not expected_mock_index
            or type(mock_runner) is not bool or mock_runner is not expected_mock_runner):
        raise ValueError("Cannot start scientific worker: its configured device or index mode differs from the deployed image and hardware grant.")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o400)
    try:
        try:
            output = os.fdopen(descriptor, "wb")
        except BaseException as failure:
            try:
                os.close(descriptor)
            except OSError as cleanup:
                raise BaseExceptionGroup("Worker configuration stream and close failed", [failure, cleanup]) from None
            raise
        with output:
            os.fchmod(output.fileno(), 0o400)
            output.write(raw)
    except BaseException as failure:
        try:
            destination.unlink()
        except OSError as cleanup:
            raise BaseExceptionGroup("Worker configuration capture and cleanup failed", [failure, cleanup]) from None
        raise


if __name__ == "__main__":
    marker = os.environ.get("SECS_MOCK_INDEX")
    runner_marker = os.environ.get("SECS_MOCK_RUNNER")
    if marker not in ("0", "1") or runner_marker not in ("0", "1"):
        raise ValueError("Cannot start scientific worker: the deployment did not select its index mode.")
    capture(SOURCE, SNAPSHOT, os.environ.get("SECS_WORKER_DEVICE"), marker == "1", runner_marker == "1")
