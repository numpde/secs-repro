#!/usr/bin/env python3
"""Download or verify the exact inert inputs admitted for the TensorRT probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request
import zipfile


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


def cached_artifact_is_admitted(output: Path, artifact: dict) -> bool:
    path = output / artifact["name"]
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == artifact["bytes"]
            and sha256(path) == artifact["sha256"]
        )
    except OSError:
        return False


def remove_owned_partials(lock: dict, output: Path) -> None:
    for artifact in lock["artifacts"]:
        (output / f".{artifact['name']}.partial").unlink(missing_ok=True)


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
    expected_names = {artifact["name"] for artifact in lock["artifacts"]}
    actual_names = {path.name for path in output.iterdir()}
    if actual_names != expected_names:
        raise ValueError(
            "The TensorRT dependency cache does not exactly match the lock: "
            f"found {sorted(actual_names)!r}, expected {sorted(expected_names)!r}."
        )
    for artifact in lock["artifacts"]:
        path = output / artifact["name"]
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Staged {artifact['name']} is not a regular owned file.")
        actual_bytes = path.stat().st_size
        if actual_bytes != artifact["bytes"]:
            raise ValueError(
                f"Staged {artifact['name']} has {actual_bytes} bytes; "
                f"expected {artifact['bytes']}."
            )
        if path.stat().st_mode & 0o044 == 0:
            raise ValueError(f"Staged {artifact['name']} is not readable by the probe user.")
        actual = sha256(path)
        if actual != artifact["sha256"]:
            raise ValueError(
                f"Staged {artifact['name']} has SHA-256 {actual}; "
                f"expected {artifact['sha256']}."
            )


def admitted_wheelhouse_wheels(wheelhouse: Path) -> list[Path]:
    manifest = wheelhouse / ".complete"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("The wheelhouse manifest is not a regular owned file.")
    entries: list[tuple[str, str]] = []
    for line in manifest.read_text().splitlines():
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or Path(name).name != name
            or not name.endswith(".whl")
        ):
            raise ValueError(f"Malformed wheelhouse manifest entry: {line!r}.")
        entries.append((name, digest))
    names = [name for name, _ in entries]
    if not names or len(names) != len(set(names)):
        raise ValueError("The wheelhouse manifest must name unique wheel files.")
    actual_names = {path.name for path in wheelhouse.iterdir() if path.name != ".complete"}
    if actual_names != set(names):
        raise ValueError(
            "The wheelhouse does not exactly match its manifest: "
            f"found {sorted(actual_names)!r}, expected {sorted(names)!r}."
        )
    wheels = []
    for name, expected_digest in entries:
        wheel = wheelhouse / name
        if wheel.is_symlink() or not wheel.is_file():
            raise ValueError(f"Wheelhouse entry {name} is not a regular owned file.")
        actual_digest = sha256(wheel)
        if actual_digest != expected_digest:
            raise ValueError(
                f"Wheelhouse entry {name} has SHA-256 {actual_digest}; "
                f"expected {expected_digest}."
            )
        wheels.append(wheel)
    return wheels


def download(lock: dict, output: Path) -> None:
    opener = urllib.request.build_opener(HttpsOnlyRedirectHandler())
    for artifact in lock["artifacts"]:
        destination = output / artifact["name"]
        # One lock-keyed cache has one writer. A deterministic partial name is
        # safe to remove on retry and makes SIGKILL recovery automatic instead
        # of leaving an anonymous entry that poisons the closed inventory.
        temporary_path = output / f".{artifact['name']}.partial"
        temporary_path.unlink(missing_ok=True)
        if cached_artifact_is_admitted(output, artifact):
            destination.chmod(0o644)
            print(f"Reusing admitted artifact {artifact['name']}.", flush=True)
            continue
        print(f"Downloading locked artifact {artifact['name']}.", flush=True)
        request = urllib.request.Request(
            artifact["url"],
            headers={"User-Agent": "secs-repro/1"},
        )
        with temporary_path.open("xb") as temporary:
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
                # These are public dependency bytes. The offline probe runs as
                # an unprivileged image user and needs read-only access later.
                temporary_path.chmod(0o644)
                temporary_path.replace(destination)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--print-required-cache-bytes", action="store_true")
    mode.add_argument("--print-required-transient-bytes", action="store_true")
    mode.add_argument("--print-required-install-bytes", action="store_true")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    if args.print_required_cache_bytes:
        if args.output is None:
            parser.error("--output is required with --print-required-cache-bytes")
        # The launcher holds the cache lock, so exact owned retry debris can be
        # removed before free space is measured without racing a downloader.
        remove_owned_partials(lock, args.output)
        # A missing or corrupt artifact needs its complete replacement beside
        # any old bytes until atomic admission. A valid cache needs only slack.
        replacement_bytes = sum(
            artifact["bytes"]
            for artifact in lock["artifacts"]
            if not cached_artifact_is_admitted(args.output, artifact)
        )
        print(replacement_bytes + 256 * 1024**2)
        return
    if args.print_required_transient_bytes:
        # The offline wheelhouse temporarily copies the admitted runtime wheels.
        # One GiB covers built meta wheels, reports, logs, and filesystem slack.
        print(sum(artifact["bytes"] for artifact in lock["artifacts"]) + 1024**3)
        return
    if args.print_required_install_bytes:
        if args.output is None:
            parser.error("--output is required with --print-required-install-bytes")
        expanded_bytes = 0
        # The completed wheelhouse, not the download lock, owns pip's complete
        # install input because it also contains wheels built offline from the
        # admitted TensorRT source archives.
        for wheel_path in admitted_wheelhouse_wheels(args.output):
            with zipfile.ZipFile(wheel_path) as wheel:
                expanded_bytes += sum(member.file_size for member in wheel.infolist())
        # pip needs a temporary extraction filesystem and a separate final
        # target. The caller gives each this measured expansion plus one GiB.
        print(expanded_bytes + 1024**3)
        return
    if args.output is None:
        parser.error("--output is required unless a required-bytes option is used")
    if not args.verify_only:
        download(lock, args.output)
    verify(lock, args.output)


if __name__ == "__main__":
    main()
