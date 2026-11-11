"""Grant one mapped container UID the read access required by a deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


_PERMISSIONS = {"--x", "r--", "rw-", "r-x", "rwx"}


def _mode(permissions: str) -> int:
    return sum(value for value, bit in ((4, "r"), (2, "w"), (1, "x")) if bit in permissions)


def _mapping_rows(project, image: str, userns_mode: str | None = None):
    """Observe one selected engine and return only after probe retirement."""
    try:
        from linux_idmap.probe import ProbeRequest, ProbeResources, ProbeFailure, probe_identity
    except ImportError as error:
        raise ValueError("Install the qualified host linux-idmap wheel before deployment.") from error

    try:
        result = probe_identity(
            ProbeRequest(
                endpoint=project.docker_endpoint, image=image,
                interpreter="/usr/local/bin/python3", user_uid=0, user_gid=0,
                userns_mode="default" if userns_mode is None else userns_mode,
                resources=ProbeResources(memory_bytes=64 * 1024**2, pids_limit=16,
                                         nano_cpus=1_000_000_000),
            ),
            executable="/usr/bin/docker",
        )
    except ProbeFailure as error:
        raise ValueError("Cannot grant deployment access: shared probe failed: "
                         + json.dumps(error.as_dict(), sort_keys=True)) from error
    return result.uid_map, result.gid_map


def mapped_ids(project, image: str, container_uid: int, container_gid: int,
               userns_mode: str | None = None) -> tuple[int, int]:
    """Translate provider IDs only after the shared probe has retired."""
    uid_map, gid_map = _mapping_rows(project, image, userns_mode)
    return int(uid_map.translate(container_uid)), int(gid_map.translate(container_gid))


def container_ids_for_host(project, image: str, host_uid: int, host_gid: int) -> tuple[int, int]:
    """Select IDs whose container writes map to the calling host owner."""
    uid_map, gid_map = _mapping_rows(project, image)
    return int(uid_map.translate_inverse(host_uid)), int(gid_map.translate_inverse(host_gid))


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise ValueError("Supply the locally pinned Python image for wheel selection.")
    from deployment.compose import ComposeProject

    repository = Path(__file__).resolve().parents[1]
    project = ComposeProject(repository, "secs-wheel-selection", ())
    uid, gid = container_ids_for_host(project, arguments[0], os.getuid(), os.getgid())
    print(f"{uid}:{gid}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Cannot select wheelhouse identity: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        raise SystemExit(2)


def _acl(path: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ("getfacl", "--physical", "--absolute-names", "--omit-header", "--numeric", "--", str(path)),
            env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot validate private deployment access for {path}: getfacl is unavailable.") from error
    if result.returncode:
        raise ValueError(f"Cannot validate private deployment access for {path}: its ACL could not be read.")
    try:
        return tuple(line for line in result.stdout.decode("ascii").splitlines() if line and not line.startswith("#"))
    except UnicodeDecodeError as error:
        raise ValueError(f"Cannot validate private deployment access for {path}: its ACL is not ASCII.") from error


def private_acl(path: Path, *, owner_permissions: str, mapped_permissions: str | None = None,
                mapped_id: int | None = None) -> bool:
    """Validate a private POSIX ACL; optionally admit one named runtime UID."""
    if (owner_permissions not in _PERMISSIONS
            or (mapped_permissions is not None and mapped_permissions not in _PERMISSIONS)):
        raise ValueError("Private deployment access requires a supported ACL permission set.")
    entries = _acl(path)
    base = [f"user::{owner_permissions}", "group::---", "other::---"]
    if entries == tuple(base):
        return (mapped_id is None and stat.S_IMODE(path.lstat().st_mode) == (_mode(owner_permissions) << 6))
    if len(entries) != 5:
        return False
    user_entry = entries[1]
    match = re.fullmatch(r"user:([0-9]+):([rwx-]{3})", user_entry)
    if match is None or entries[0] != f"user::{owner_permissions}" or entries[2] != "group::---" or entries[4] != "other::---":
        return False
    identifier, permissions = int(match[1]), match[2]
    if (permissions not in _PERMISSIONS or entries[3] != f"mask::{permissions}"
            or (mapped_id is not None and identifier != mapped_id)
            or (mapped_permissions is not None and permissions != mapped_permissions)):
        return False
    return stat.S_IMODE(path.lstat().st_mode) == ((_mode(owner_permissions) << 6) | (_mode(permissions) << 3))


def private_owner_acl(path: Path, *, owner_permissions: str) -> bool:
    """Require an owner-only ACL with no named entries."""
    expected = (f"user::{owner_permissions}", "group::---", "other::---")
    return (_acl(path) == expected
            and stat.S_IMODE(path.lstat().st_mode) == (_mode(owner_permissions) << 6))


def mapped_acl_permissions(path: Path, owner_permissions: str, mapped_id: int) -> str | None:
    """Return one validated mapped-user ACL entry, or None for every other ACL."""
    entries = _acl(path)
    if len(entries) != 5 or entries[0] != f"user::{owner_permissions}" or entries[2] != "group::---" or entries[4] != "other::---":
        return None
    match = re.fullmatch(r"user:([0-9]+):([rwx-]{3})", entries[1])
    if match is None or int(match[1]) != mapped_id:
        return None
    permissions = match[2]
    if (permissions not in _PERMISSIONS or entries[3] != f"mask::{permissions}"
            or stat.S_IMODE(path.lstat().st_mode) != ((_mode(owner_permissions) << 6) | (_mode(permissions) << 3))):
        return None
    return permissions


def set_mapped_access(path: Path, mapped_id: int, permissions: str, *, owner_uid: int,
                      owner_permissions: str, directory: bool) -> None:
    """Set one exact private named-user ACL, changing no group/other entry."""
    existing = mapped_acl_permissions(path, owner_permissions, mapped_id)
    if existing is None:
        grant_private_access(path, mapped_id, permissions, owner_uid=owner_uid,
                             owner_permissions=owner_permissions, directory=directory)
        return
    if path.lstat().st_uid != owner_uid:
        raise ValueError(f"Cannot change deployment access for {path}: its owner differs from the expected owner.")
    if existing == permissions:
        return
    try:
        result = subprocess.run(
            ("setfacl", "--physical", "--modify", f"u:{mapped_id}:{permissions},m::{permissions}", "--", str(path)),
            env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot narrow deployment access for {path}: setfacl is unavailable.") from error
    if result.returncode or mapped_acl_permissions(path, owner_permissions, mapped_id) != permissions:
        raise ValueError(f"Cannot narrow deployment access for {path}; the resulting ACL did not match the grant.")


def grant_private_access(path: Path, mapped_id: int, permissions: str, *, owner_uid: int,
                         owner_permissions: str, directory: bool) -> None:
    """Add one named-UID ACL without changing ownership or widening group/other access."""
    if permissions not in _PERMISSIONS:
        raise ValueError("Cannot grant deployment input access with unsupported permissions.")
    metadata = path.lstat()
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (path.resolve() != path or not expected_type or metadata.st_uid != owner_uid):
        raise ValueError(f"Cannot grant deployment input access: {path} must be a regular owner-controlled path.")
    observed = _acl(path)
    expected_owner_acl = (f"user::{owner_permissions}", "group::---", "other::---")
    intended_acl = (f"user::{owner_permissions}", f"user:{mapped_id}:{permissions}",
                    "group::---", f"mask::{permissions}", "other::---")
    if observed == intended_acl:
        return
    if observed != expected_owner_acl or stat.S_IMODE(metadata.st_mode) != (_mode(owner_permissions) << 6):
        raise ValueError(f"Cannot grant deployment input access: {path} already has an unexpected ACL.")
    try:
        result = subprocess.run(
            ("setfacl", "--physical", "--modify", f"u:{mapped_id}:{permissions},m::{permissions}", "--", str(path)),
            env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Cannot grant deployment input access to {path}: setfacl is unavailable.") from error
    if result.returncode or _acl(path) != intended_acl:
        raise ValueError(f"Cannot grant only mapped-UID access to {path}; the resulting ACL did not match the grant.")

