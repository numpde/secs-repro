#!/usr/bin/env python3
"""Admit one file from the pinned SECS archive.

The archive is never materialized. The selected member remains temporary until
the complete compressed stream matches the expected digest; only then are its
bytes emitted to the networkless consumer.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request


READ_BYTES = 1024 * 1024
PROGRESS_BYTES = 512 * 1024 * 1024


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep every redirect hop inside the HTTPS transport boundary."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        target = urllib.parse.urljoin(request.full_url, new_url)
        if urllib.parse.urlsplit(target).scheme != "https":
            raise ValueError(f"redirect target is not HTTPS: {target}")
        return super().redirect_request(
            request, file_pointer, code, message, headers, target
        )


class MeasuredReader:
    """Hash compressed bytes as the streaming tar reader consumes them."""

    def __init__(self, source):
        self.source = source
        self.digest = hashlib.md5(usedforsecurity=False)
        self.count = 0
        self.next_progress = PROGRESS_BYTES

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(READ_BYTES if size < 0 else min(size, READ_BYTES))
        if chunk:
            self.digest.update(chunk)
            self.count += len(chunk)
            if self.count >= self.next_progress:
                print(f"read {self.count} compressed archive bytes", file=sys.stderr)
                self.next_progress += PROGRESS_BYTES
        return chunk


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--archive", type=Path)
    source.add_argument("--url")
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="Read the archive URL, digest, and default member path from this TOML file.",
    )
    parser.add_argument(
        "--member-spec",
        type=Path,
        help="Override only the selected member path using this TOML file.",
    )
    parser.add_argument("--scratch-directory", type=Path, required=True)
    return parser.parse_args()


def open_url(url: str):
    """Open an HTTPS archive stream with HTTPS-only redirects."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("archive URL must be an absolute HTTPS URL")
    request = urllib.request.Request(url, headers={"User-Agent": "secs-repro/1"})
    response = urllib.request.build_opener(HttpsOnlyRedirectHandler()).open(
        request, timeout=60
    )
    return response


def normalized_member_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def copy_member(archive: tarfile.TarFile, member: tarfile.TarInfo, destination) -> None:
    source = archive.extractfile(member)
    with source:
        while chunk := source.read(READ_BYTES):
            destination.write(chunk)


def open_archive_source(args: argparse.Namespace, default_url: str):
    """Select the read-only local source or the network source."""

    if args.archive is not None:
        return args.archive.open("rb")
    return open_url(args.url or default_url)


def stage_member(
    archive: tarfile.TarFile,
    member_name: str,
    scratch_directory: Path,
) -> Path:
    """Copy the archive's unique expected regular member to a temporary file."""

    temp_path: Path | None = None
    try:
        for member in archive:
            name = normalized_member_name(member.name)
            if name != member_name:
                continue
            if temp_path is not None:
                raise ValueError(
                    f"archive contains more than one member named {member_name!r}"
                )
            if not member.isfile():
                raise ValueError(f"archive member is not a regular file: {member.name}")
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix="archive-member.",
                dir=scratch_directory,
                delete=False,
            ) as destination:
                temp_path = Path(destination.name)
                copy_member(archive, member, destination)
                destination.flush()
        if temp_path is None:
            raise ValueError(f"archive has no member named {member_name!r}")
        return temp_path
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def verify_archive(reader: MeasuredReader, expected_md5: str) -> None:
    """Consume any compressed tail and admit the complete archive digest."""

    # Tar iteration can stop before the gzip trailer or trailing archive bytes.
    # Drain the source so the digest always covers the complete supplied file.
    while reader.read(READ_BYTES):
        pass
    actual_md5 = reader.digest.hexdigest()
    if actual_md5 != expected_md5.lower():
        raise ValueError(
            f"archive MD5 is {actual_md5}; expected {expected_md5.lower()}"
        )


def extract(args: argparse.Namespace) -> None:
    """Stage and verify the requested member before emitting any bytes."""

    temp_path: Path | None = None
    try:
        with args.spec.open("rb") as source:
            archive_spec = tomllib.load(source)["archive"]
        member_name = archive_spec["member"]
        if args.member_spec is not None:
            with args.member_spec.open("rb") as source:
                member_name = tomllib.load(source)["archive"]["member"]
        with open_archive_source(args, archive_spec["url"]) as raw_source:
            measured = MeasuredReader(raw_source)
            # Stream mode bounds memory, and copy_member deliberately applies no
            # archive path, permissions, ownership, or other tar metadata.
            with tarfile.open(fileobj=measured, mode="r|gz") as archive:
                temp_path = stage_member(
                    archive,
                    member_name,
                    args.scratch_directory,
                )
            verify_archive(measured, archive_spec["md5"])

        with temp_path.open("rb") as selected_member:
            while chunk := selected_member.read(READ_BYTES):
                sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    extract(arguments())
