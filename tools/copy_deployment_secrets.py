"""Copy installed provider secrets into files owned by the mapped runtime UID."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys


_FILES = {"provider.signing.private.json", "interpreter.key"}
_LIMIT = 65536


def _read_existing(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _LIMIT:
            raise ValueError("An installed deployment secret is not a bounded regular file.")
        content = bytearray()
        while len(content) <= _LIMIT:
            part = os.read(descriptor, _LIMIT + 1 - len(content))
            if not part:
                break
            content.extend(part)
        if len(content) != metadata.st_size or len(content) > _LIMIT:
            raise ValueError("An installed deployment secret changed length while it was copied.")
        return bytes(content)
    finally:
        os.close(descriptor)


def _publish(directory: Path, name: str, content: bytes) -> str:
    destination = directory / name
    try:
        existing = _read_existing(destination)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        metadata = destination.lstat()
        if (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o400
                or existing != content):
            raise ValueError(f"Cannot reuse deployment-owned runtime secret {name!r}; its owner, mode, or bytes differ.")
        return "reused"

    temporary = directory / f".{name}.stage-{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    published = False
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("A deployment secret copy made no write progress.")
            view = view[written:]
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, destination, follow_symlinks=False)
        published = True
        os.unlink(temporary)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return "created"


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        raise ValueError("The deployment secret copier requires a destination and one supported filename.")
    directory = Path(arguments[0])
    name = arguments[1]
    status = directory.lstat()
    if (name not in _FILES or not stat.S_ISDIR(status.st_mode) or os.geteuid() == 0
            or not os.access(directory, os.W_OK | os.X_OK, effective_ids=True)):
        raise ValueError("The runtime secret directory must grant the nonroot container user write and traversal access.")
    content = sys.stdin.buffer.read(_LIMIT + 1)
    if not 0 < len(content) <= _LIMIT:
        raise ValueError("The deployment secret input must contain 1 to 65536 bytes.")
    print(json.dumps({name: _publish(directory, name, content)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
