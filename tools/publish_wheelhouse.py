"""Publish a completed Linux wheelhouse without deleting its incumbent first.

After replacement the stage path names the old wheelhouse; the caller removes
it only after publication succeeds. This gives atomic visibility, not a claim
of power-loss durability. The producer owns validation of the staged wheels.
"""

import ctypes
import os
from pathlib import Path
import stat
import sys


_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


def _exchange(stage, destination):
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(_AT_FDCWD, os.fsencode(stage), _AT_FDCWD, os.fsencode(destination), _RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "Cannot publish wheelhouse: " + os.strerror(error), str(stage), None, str(destination))


def publish(stage: Path, destination: Path):
    staged = stage.lstat()
    if not stat.S_ISDIR(staged.st_mode):
        raise ValueError("Cannot publish wheelhouse: the completed stage must be a real directory")
    try:
        incumbent = destination.lstat()
    except FileNotFoundError:
        os.rename(stage, destination)
        return
    if not stat.S_ISDIR(incumbent.st_mode):
        raise ValueError("Cannot publish wheelhouse: the existing destination must be a real directory")
    if (staged.st_dev, staged.st_ino) == (incumbent.st_dev, incumbent.st_ino):
        raise ValueError("Cannot publish wheelhouse: staging and published paths name the same directory")
    _exchange(stage, destination)


if __name__ == "__main__":
    publish(Path(sys.argv[1]), Path(sys.argv[2]))
