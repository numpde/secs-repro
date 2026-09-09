"""Publish private deployment inputs without replacing existing files.

Adapted from numpde/nmrpeak-repro's deployment/provider_deployment.py at
ae53376b9bb1dc572d3d7bce6592358080e08b18. The caller selects the templates;
this module owns publication, permissions, and concurrent initializer exclusion.
Configuration templates come from one committed snapshot; individual secrets
and ownership records are published only after their complete bytes are synced.
It neither knows model layouts nor contacts Docker or the API.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from collections.abc import Iterator, Mapping


_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def initialize_configuration(
    repository: Path, name: str, templates: Mapping[str, Path]
) -> Path:
    """Publish a new mode-0700 config directory, without replacing existing input.

    Template bytes come from a single HEAD revision, not the working tree.
    Values name tracked regular files; keys are flat destination filenames.
    Config files are mode 0600. Cooperating initializers lock the parent until
    publication is durable. Other processes running as the owner are trusted.
    No credentials, runtime state, containers, or downloads are created.
    """
    destination = configuration_directory(repository, name)
    root = repository.resolve(strict=True)
    config = root / "config"
    if config.is_symlink() or not config.is_dir():
        raise ValueError("Cannot initialize deployment: config must be a real directory.")
    if not templates:
        raise ValueError("Cannot initialize deployment without configuration templates.")
    revision = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    contents = {}
    for filename, relative in templates.items():
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise ValueError("Configuration template destinations must be flat filenames.")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Configuration template sources must be repository-relative.")
        contents[filename] = _committed_template(root, revision, relative)

    parent = config / "deployments"
    _ensure_private_parent(parent)
    with _locked_parent(parent) as parent_fd:
        # Test without following links: a dangling link also reserves this name.
        if os.path.lexists(destination):
            raise FileExistsError(
                f"Deployment configuration already exists: {destination}. "
                "Initialization will not replace it."
            )
        with TemporaryDirectory(prefix=f".{name}.", dir=parent) as temporary:
            stage = Path(temporary)
            for filename, content in contents.items():
                _write_new_file(stage / filename, content)
            _sync_directory(stage)
            stage.rename(destination)
            # After rename the configuration is visible. A failed fsync does
            # not undo publication; never delete the destination as rollback.
            try:
                os.fsync(parent_fd)
            except OSError as error:
                error.add_note(
                    f"Configuration is visible at {destination}, but crash durability "
                    "could not be confirmed. It has not been removed."
                )
                raise
    return destination


def configuration_directory(repository: Path, name: str) -> Path:
    """Resolve a literal deployment name without allowing it to select another directory."""
    if _NAME.fullmatch(name) is None:
        raise ValueError(
            "Cannot select deployment: use 1–64 lowercase letters, digits, "
            "or hyphens, starting and ending with a letter or digit."
        )
    return repository.resolve(strict=True) / "config/deployments" / name


def _git(repository: Path, *arguments: str) -> bytes:
    """Read this repository with ambient Git redirection and config removed."""
    try:
        result = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": "/dev/null"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Reading committed deployment templates timed out after 30 seconds.") from error
    if result.returncode:
        raise ValueError(
            "Cannot read committed deployment templates; check that this is a Git "
            "checkout with a committed HEAD and the required template files."
        )
    return result.stdout


def _committed_template(repository: Path, revision: str, relative: Path) -> bytes:
    """Read a regular Git blob from the pinned revision, never a symlink target."""
    entry = _git(repository, "ls-tree", revision, "--", relative.as_posix())
    fields = entry.split(maxsplit=3)
    if len(fields) != 4 or fields[:2] != [b"100644", b"blob"]:
        raise ValueError(f"Deployment template is not a committed regular file: {relative}")
    content = _git(repository, "cat-file", "blob", fields[2].decode("ascii"))
    if not content:
        raise ValueError(f"Deployment template is empty: {relative}")
    return content


def _ensure_private_parent(parent: Path) -> None:
    """Create private storage or reject an existing directory with broader access."""
    try:
        parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError(
            f"Cannot initialize under {parent}: it must be a real, "
            "operator-owned directory with mode 0700."
        )
    _sync_directory(parent.parent)


@contextmanager
def _locked_parent(parent: Path) -> Iterator[int]:
    """Serialize publication through the parent inode, without a removable lock file."""
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, content: bytes) -> None:
    """Write and sync one owner-only file inside the unpublished staging directory."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _publish_new_file(path: Path, content: bytes) -> None:
    """Publish complete owner-only bytes in an existing private parent, without replacement."""
    with TemporaryDirectory(prefix=".install-", dir=path.parent) as temporary:
        staged = Path(temporary) / path.name
        _write_new_file(staged, content)
        # A failed write must not reserve the destination with partial bytes.
        # Linking also preserves an existing credential or ownership record.
        os.link(staged, path)
        try:
            _sync_directory(path.parent)
        except OSError as error:
            error.add_note(f"File is visible at {path}; crash durability is unconfirmed.")
            raise


def _sync_directory(path: Path) -> None:
    """Persist directory entries on the supported local Linux filesystem."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
