"""Capture package image source identities and checksum metadata."""

import hashlib
import json
from pathlib import Path
import stat
import sys


def main(stage, repository, variant):
    stage, repository = Path(stage), Path(repository)
    selections = []
    files = []
    for role in ('packages', 'native'):
        entries = json.loads((stage / f'{role}.json').read_text())
        selections.extend(entries)
        checksums = ''.join(f"{item['sha256']}  {item['filename']}\n" for item in sorted(entries, key=lambda x: x['filename']))
        checksum_file = stage / f'{role}.complete'
        checksum_file.write_text(checksums)
        files.append((checksum_file, f'{role}_wheels/.complete'))
    (stage / 'manifest.json').write_text(json.dumps(selections, sort_keys=True))
    files.extend([(stage / 'packages.lock', f'requirements/packages-{variant}.lock'),
                  (stage / 'native.lock', 'requirements/native.lock')])
    for name in ('containers/packages/Dockerfile', 'pyproject.toml', 'secs/pyproject.toml', 'secs/README.md'):
        files.append((repository / name, name))
    for name in ('src', 'secs/src'):
        for path in sorted((repository / name).rglob('*')):
            if path.is_symlink():
                raise ValueError(f'Package source is a symlink: {path}')
            if path.is_file():
                files.append((path, path.relative_to(repository).as_posix()))
    entries = []
    for path, destination in files:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f'Package input is not a regular file: {path}')
        entries.append({'path': str(path), 'destination': destination,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'executable': bool(metadata.st_mode & 0o111)})
    (stage / 'sources.json').write_text(json.dumps(entries, sort_keys=True))
    identity = [{key: value for key, value in entry.items() if key != 'path'} for entry in entries]
    print(hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest())


if __name__ == '__main__':
    main(*sys.argv[1:])
