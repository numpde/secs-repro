"""Select a CPU or GPU target explicitly or through the controlling terminal."""

import os
import sys


def worker_target(value: str) -> str:
    if not value:
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                terminal.write("Select worker target:\n  1) cpu\n  2) gpu\nChoice [1-2]: ")
                terminal.flush()
                answer = terminal.readline().strip()
        except OSError as error:
            raise ValueError("Select TARGET=cpu or TARGET=gpu on the make command line; no interactive terminal is available.") from error
        value = {"1": "cpu", "2": "gpu"}.get(answer, "")
    if value not in ("cpu", "gpu"):
        raise ValueError("Worker target must be cpu or gpu.")
    return value


def main():
    try:
        print(worker_target(os.environ.get("SECS_WORKER_TARGET", "")))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
