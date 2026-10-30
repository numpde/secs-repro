"""Capture the worker configuration once for the native supervisor lifetime."""

import os
from pathlib import Path
import tomllib

from provider_core.startup_file import read_startup_file


SOURCE = Path("/run/config/worker/worker.toml")
SNAPSHOT = Path("/tmp/native-config/worker.toml")


def capture(source, destination, expected_device):
    raw = read_startup_file(source, maximum_bytes=65536, label="Scientific worker configuration")
    try:
        device = tomllib.loads(raw.decode("utf-8")).get("device", "cuda:0")
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Cannot capture scientific worker configuration: invalid TOML.") from error
    if expected_device not in ("cpu", "cuda:0") or device != expected_device:
        raise ValueError("Cannot start scientific worker: its configured device differs from the deployed image and hardware grant.")
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
    capture(SOURCE, SNAPSHOT, os.environ.get("SECS_WORKER_DEVICE"))
