"""Publish verified staged artifacts individually without replacing a shared pool."""

import hashlib
import os
from pathlib import Path
import stat
import sys


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').digest()


def publish(stage, pool):
    for source in sorted(stage.iterdir()):
        if source.name.startswith('.'):
            continue
        if source.is_symlink() and os.readlink(source) == '/pool/' + source.name:
            continue
        if not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError('Wheel stage contains a non-regular artifact')
        destination = pool / source.name
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            if not stat.S_ISREG(destination.lstat().st_mode) or digest(destination) != digest(source):
                raise ValueError(f'Shared wheel already exists with different bytes: {source.name}') from None


if __name__ == '__main__':
    publish(Path(sys.argv[1]), Path(sys.argv[2]))
