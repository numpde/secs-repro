"""Initialize SECS-owned volume roots and the native session lock."""

import os
from pathlib import Path
import stat
import sys


def initialize_sources(path, uid, gid):
    facts = path.lstat()
    if not stat.S_ISDIR(facts.st_mode):
        raise ValueError("SECS source volume must be a directory")
    if (facts.st_uid, facts.st_gid, stat.S_IMODE(facts.st_mode)) == (uid, gid, 0o700):
        return
    if facts.st_uid != 0 or any(path.iterdir()):
        raise ValueError("Cannot initialize a populated or foreign SECS source volume")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o700)
        os.fchown(fd, uid, gid)
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize_session(path):
    lock = path / ".owner-session.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        facts = lock.lstat()
        if not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.geteuid() or stat.S_IMODE(facts.st_mode) != 0o600:
            raise ValueError("SECS native lifetime lock must be an owner-only regular file") from None
    else:
        os.close(fd)


if __name__ == "__main__":
    if sys.argv[1] == "sources":
        initialize_sources(Path("/sources"), int(sys.argv[2]), int(sys.argv[3]))
        initialize_sources(Path("/session"), int(sys.argv[2]), int(sys.argv[3]))
    elif sys.argv[1] == "session":
        initialize_session(Path("/session"))
    else:
        raise ValueError("Unknown SECS volume initializer")
