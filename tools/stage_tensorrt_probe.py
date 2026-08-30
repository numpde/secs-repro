#!/usr/bin/env python3
"""Download or verify the exact inert inputs admitted for the TensorRT probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request


READ_BYTES = 1024 * 1024
PROGRESS_BYTES = 256 * 1024 * 1024


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep every redirect hop inside the HTTPS transport boundary."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        target = urllib.parse.urljoin(request.full_url, new_url)
        if urllib.parse.urlsplit(target).scheme != "https":
            raise ValueError(f"Download redirect target is not HTTPS: {target}")
        return super().redirect_request(request, file_pointer, code, message, headers, target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(READ_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path: Path) -> dict:
    lock = json.loads(path.read_text())
    if lock.get("kind") != "secs.tensorrt-probe-downloads.v1":
        raise ValueError(f"Unsupported TensorRT probe lock kind in {path}.")
    names = [artifact["name"] for artifact in lock["artifacts"]]
    if len(names) != len(set(names)):
        raise ValueError("TensorRT probe artifact names must be unique.")
    for artifact in lock["artifacts"]:
        parsed = urllib.parse.urlsplit(artifact["url"])
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"Artifact URL must be absolute HTTPS: {artifact['url']!r}.")
        if Path(urllib.parse.unquote(parsed.path)).name != artifact["name"]:
            raise ValueError(f"Artifact URL does not end with its locked name: {artifact!r}.")
        if len(artifact["sha256"]) != 64:
            raise ValueError(f"Artifact SHA-256 is malformed: {artifact!r}.")
        if not isinstance(artifact["bytes"], int) or artifact["bytes"] <= 0:
            raise ValueError(f"Artifact byte count is malformed: {artifact!r}.")
        if artifact["role"] == "molformer-source":
            repository_path = urllib.parse.urlsplit(artifact["repository"]).path.strip("/")
            expected_url = (
                f"https://raw.githubusercontent.com/{repository_path}/"
                f"{artifact['revision']}/{artifact['name']}"
            )
            if artifact["url"] != expected_url:
                raise ValueError(
                    "The MolFormer source URL is not bound to its recorded revision: "
                    f"{artifact!r}."
                )
    return lock


def verify(lock: dict, output: Path) -> None:
    for artifact in lock["artifacts"]:
        path = output / artifact["name"]
        actual = sha256(path)
        if actual != artifact["sha256"]:
            raise ValueError(
                f"Staged {artifact['name']} has SHA-256 {actual}; "
                f"expected {artifact['sha256']}."
            )


def download(lock: dict, output: Path) -> None:
    opener = urllib.request.build_opener(HttpsOnlyRedirectHandler())
    for artifact in lock["artifacts"]:
        print(f"Downloading locked artifact {artifact['name']}.", flush=True)
        request = urllib.request.Request(
            artifact["url"],
            headers={"User-Agent": "secs-repro/1"},
        )
        with tempfile.NamedTemporaryFile(dir=output, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                byte_count = 0
                next_progress = PROGRESS_BYTES
                with opener.open(request, timeout=60) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) != artifact["bytes"]:
                        raise ValueError(
                            f"Server reports {content_length} bytes for {artifact['name']}; "
                            f"expected {artifact['bytes']}."
                        )
                    while chunk := response.read(READ_BYTES):
                        temporary.write(chunk)
                        byte_count += len(chunk)
                        if byte_count > artifact["bytes"]:
                            raise ValueError(
                                f"Downloaded {artifact['name']} exceeds its locked "
                                f"{artifact['bytes']} bytes."
                            )
                        if byte_count >= next_progress:
                            print(
                                f"Read {byte_count} bytes for {artifact['name']}.",
                                flush=True,
                            )
                            next_progress += PROGRESS_BYTES
                temporary.flush()
                if byte_count != artifact["bytes"]:
                    raise ValueError(
                        f"Downloaded {artifact['name']} has {byte_count} bytes; "
                        f"expected {artifact['bytes']}."
                    )
                actual = sha256(temporary_path)
                if actual != artifact["sha256"]:
                    raise ValueError(
                        f"Downloaded {artifact['name']} has SHA-256 {actual}; "
                        f"expected {artifact['sha256']}."
                    )
                temporary_path.replace(output / artifact["name"])
                print(
                    f"Admitted {artifact['name']} ({byte_count} bytes).",
                    flush=True,
                )
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--print-required-host-bytes", action="store_true")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    if args.print_required_host_bytes:
        # Downloads and the finished wheelhouse coexist until the one-shot run
        # ends. Two extra GiB cover built meta wheels, logs, and filesystem slack.
        print(2 * sum(artifact["bytes"] for artifact in lock["artifacts"]) + 2 * 1024**3)
        return
    if args.output is None:
        parser.error("--output is required unless --print-required-host-bytes is used")
    if not args.verify_only:
        download(lock, args.output)
    verify(lock, args.output)


if __name__ == "__main__":
    main()
