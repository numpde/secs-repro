"""Acquire one small pinned release asset into an existing shared wheelhouse."""

import argparse
import fcntl
import hashlib
import http.client
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

from tools.extract_checkpoint import open_url

MAX_ASSET_BYTES = 16 * 1024 * 1024
DOWNLOAD_SECONDS = 300


def check(directory, name, expected):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Cannot use {name}: expected a regular file.")
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"Cannot use {name}: cached bytes differ; the existing file is preserved.")
    return True


def acquire(directory, name, expected, url):
    temporary = f".release-{secrets.token_hex(16)}.part"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            digest = hashlib.sha256()
            deadline = time.monotonic() + DOWNLOAD_SECONDS
            try:
                source = open_url(url)
            except (OSError, ValueError, http.client.HTTPException) as error:
                raise OSError(f"Cannot acquire {name}: the download failed.") from error
            size = 0
            with source:
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Cannot acquire {name}: the download deadline elapsed.")
                    try:
                        block = source.read1(1024 * 1024)
                    except (OSError, http.client.HTTPException) as error:
                        raise OSError(f"Cannot acquire {name}: the download failed.") from error
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_ASSET_BYTES:
                        raise ValueError(f"Cannot acquire {name}: the release asset exceeds 16 MiB.")
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"Cannot acquire {name}: downloaded bytes differ from the pinned hash.")
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if not check(directory, name, expected):
                raise FileNotFoundError(f"Cannot acquire {name}: the competing file disappeared.")
        os.fsync(directory)
    finally:
        os.unlink(temporary, dir_fd=directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "acquire"))
    parser.add_argument("pool", type=Path)
    parser.add_argument("name")
    parser.add_argument("sha256")
    parser.add_argument("url")
    args = parser.parse_args()
    if (not args.name or args.name in (".", "..") or "/" in args.name or "\\" in args.name
            or not re.fullmatch(r"[0-9a-f]{64}", args.sha256)):
        parser.error("supply a single filename and a lowercase SHA-256 hash")
    try:
        directory = os.open(args.pool, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX if args.action == "acquire" else fcntl.LOCK_SH)
            if check(directory, args.name, args.sha256):
                return 0
            if args.action == "check":
                return 10
            acquire(directory, args.name, args.sha256, args.url)
            return 0
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
