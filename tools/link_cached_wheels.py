"""Let pip reuse shared artifacts in its private download destination."""

import os
from pathlib import Path
import stat
import sys


def link(pool, stage):
    for source in pool.iterdir():
        if not (source.name.endswith('.whl') or source.name.endswith('.tar.gz')):
            continue
        if stat.S_ISREG(source.lstat().st_mode):
            os.symlink('/pool/' + source.name, stage / source.name)


if __name__ == '__main__':
    link(Path(sys.argv[1]), Path(sys.argv[2]))
