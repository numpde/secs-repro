#!/usr/bin/env python3
"""Verify or install the pinned SDK acquisition source bundle offline."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / 'contracts/upstream/provider_core_release.json'
BUNDLE = ROOT / 'tools/bootstrap/acquisition'
LOCKS = (ROOT / 'requirements/provider.lock', ROOT / 'requirements/native.lock')


def pinned_release(record):
    matches = []
    for path in LOCKS:
        matches.append(re.findall(r'^provider-core==([0-9]+\.[0-9]+\.[0-9]+) \\\n\s+--hash=sha256:([0-9a-f]{64})$', path.read_text(), re.MULTILINE))
    if any(len(match) != 1 for match in matches) or matches[0] != matches[1]:
        raise ValueError('Acquisition bootstrap requires matching exact provider-core pins in both locks.')
    version, checksum = matches[0][0]
    if record['files'].get(f'provider_core-{version}-py3-none-any.whl') != checksum:
        raise ValueError('Acquisition bootstrap release record differs from the provider-core locks.')
    return f'acquisition-owner-{version}.tar.gz'


def inventory(record):
    entries = record.get('acquisition_source_files')
    if not isinstance(entries, dict) or not entries:
        raise ValueError('The pinned SDK release has no acquisition bootstrap inventory.')
    for name, entry in entries.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or path.as_posix() != name
                or not isinstance(entry, dict) or set(entry) != {'sha256', 'size', 'mode'}
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                or type(entry['size']) is not int or entry['size'] < 0 or entry['mode'] != 0o644):
            raise ValueError(f'Invalid acquisition bootstrap inventory entry: {name}')
    return entries


def check_bytes(name, data, entry):
    if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError(f'Acquisition bootstrap file differs from its release record: {name}')


def verify(bundle, entries):
    if any(path.is_symlink() for path in (bundle.parent.parent, bundle.parent, bundle)) or not bundle.is_dir():
        raise ValueError('Acquisition bootstrap must be a directory without a symlink.')
    actual = set()
    for current, directories, files in os.walk(bundle, followlinks=False):
        for directory in directories:
            if (Path(current) / directory).is_symlink():
                raise ValueError('Acquisition bootstrap contains a symlink directory.')
        for filename in files:
            path = Path(current) / filename
            name = path.relative_to(bundle).as_posix()
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o111:
                raise ValueError(f'Acquisition bootstrap member is not a non-executable regular file: {name}')
            actual.add(name)
    if actual != set(entries):
        raise ValueError('Acquisition bootstrap files differ from the release inventory.')
    for name, entry in entries.items():
        check_bytes(name, (bundle / name).read_bytes(), entry)


def previous_inventory():
    try:
        data = subprocess.check_output(
            ['git', '-C', str(ROOT), 'show', 'HEAD:contracts/upstream/provider_core_release.json'],
            stderr=subprocess.DEVNULL,
        )
        return inventory(json.loads(data))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise ValueError('Cannot verify the installed acquisition bootstrap against the committed release.') from error


def exchange_directories(first, second):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def install(archive_path, record, entries, name):
    if archive_path.name != name or hashlib.sha256(archive_path.read_bytes()).hexdigest() != record['files'].get(name):
        raise ValueError('Acquisition bootstrap archive differs from the pinned SDK release.')
    if any(path.is_symlink() for path in (BUNDLE.parent.parent, BUNDLE.parent, BUNDLE)):
        raise ValueError('Acquisition bootstrap path cannot contain a symlink.')
    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    if BUNDLE.exists() and not BUNDLE.is_dir():
        raise ValueError('The existing acquisition bootstrap is not a directory.')
    if BUNDLE.exists():
        try:
            verify(BUNDLE, entries)
            return
        except ValueError:
            verify(BUNDLE, previous_inventory())
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) != len(entries) or {member.name for member in members} != set(entries):
            raise ValueError('Acquisition bootstrap archive differs from its release inventory.')
        contents = {}
        for member in members:
            if not member.isfile() or member.mode != 0o644:
                raise ValueError(f'Invalid acquisition bootstrap archive member: {member.name}')
            with archive.extractfile(member) as source:
                contents[member.name] = source.read()
            check_bytes(member.name, contents[member.name], entries[member.name])
    stage = Path(tempfile.mkdtemp(prefix='.acquisition.', dir=BUNDLE.parent))
    try:
        for name, data in contents.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        verify(stage, entries)
        if BUNDLE.exists():
            exchange_directories(stage, BUNDLE)
        else:
            stage.rename(BUNDLE)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='install the exact release bundle into an absent snapshot')
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    name = pinned_release(record)
    entries = inventory(record)
    if args.archive:
        install(args.archive, record, entries, name)
    verify(BUNDLE, entries)


if __name__ == '__main__':
    main()
