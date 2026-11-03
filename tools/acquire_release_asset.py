"""Acquire one small pinned release asset into an existing shared wheelhouse."""

import argparse
import errno
import fcntl
import hashlib
import http.client
import os
from pathlib import Path
import re
import secrets
import stat
import socket
import ssl
import sys
import time
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from tools.extract_checkpoint import open_url

MAX_ASSET_BYTES = 16 * 1024 * 1024
DOWNLOAD_SECONDS = 300


def network_failure(error):
    """Match the provider-tools wheelhouse_tool/network.py retry policy."""
    if isinstance(error, urllib.error.HTTPError):
        delay = None
        if error.code in (429, 503):
            value = error.headers.get("Retry-After") if error.headers else None
            if value:
                try:
                    if value.isascii() and value.isdecimal():
                        digits = value.lstrip("0") or "0"
                        delay = 31 if len(digits) > 2 else int(digits)
                    else:
                        delay = max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return error.code in (408, 429, 500, 502, 503, 504), f"HTTP {error.code}", delay
    if isinstance(error, urllib.error.URLError):
        return network_failure(error.reason) if isinstance(error.reason, BaseException) else (False, "network request failed", None)
    if isinstance(error, ssl.SSLError):
        return False, "TLS error", None
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN, "DNS error", None
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True, "network timeout", None
    if isinstance(error, (http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True, "connection interrupted", None
    if isinstance(error, OSError):
        if match := re.match(r"^Tunnel connection failed: ([0-9]{3})(?:\s|$)", str(error)):
            status = int(match[1])
            if status in (429, 503):
                return False, f"proxy tunnel HTTP {status}; retry delay unavailable", None
            return status in (408, 500, 502, 504), f"proxy tunnel HTTP {status}", None
        return error.errno in (errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPIPE), f"network error (errno {error.errno})" if isinstance(error.errno, int) else "network error", None
    return False, "network request failed", None


class NetworkFailure(Exception):
    pass


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
                raise NetworkFailure(error) from error
            size = 0
            with source:
                while True:
                    if time.monotonic() >= deadline:
                        raise NetworkFailure(TimeoutError())
                    try:
                        block = source.read1(1024 * 1024)
                    except (OSError, http.client.HTTPException) as error:
                        raise NetworkFailure(error) from error
                    if not block:
                        if isinstance(getattr(source, "length", None), int) and source.length > 0:
                            raise NetworkFailure(http.client.IncompleteRead(b""))
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


def acquire_with_retry(directory, name, expected, url):
    for attempt in range(1, 4):
        try:
            acquire(directory, name, expected, url)
            return
        except NetworkFailure as failure:
            retry, cause, retry_after = network_failure(failure.args[0])
            if retry_after is not None and retry_after > 30:
                raise OSError(f"Cannot acquire {name}: {cause}; Retry-After exceeds 30 seconds.") from failure
            if not retry or attempt == 3:
                raise OSError(f"Cannot acquire {name}: download failed after {attempt} attempt(s): {cause}.") from failure
            time.sleep(max(attempt, retry_after or 0))


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
            acquire_with_retry(directory, args.name, args.sha256, args.url)
            return 0
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
