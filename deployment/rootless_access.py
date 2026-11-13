"""Grant one mapped container UID the read access required by a deployment."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import sys


_PERMISSIONS = {"--x", "r--", "rw-", "r-x", "rwx"}


def _mode(permissions: str) -> int:
    return sum(value for value, bit in ((4, "r"), (2, "w"), (1, "x")) if bit in permissions)


def _mapped_ids(project, image: str, uid: int, gid: int, *, inverse: bool,
                userns_mode: str | None = None) -> tuple[int, int]:
    """Use the verified shared probe on the selected engine and translate both IDs."""
    bundle = project.repository / "tools/bootstrap/acquisition"
    verifier = bundle / "acquisition_tool/source_update.py"
    verified = subprocess.run((sys.executable, "-I", "-S", "-B", str(verifier), "verify",
                               "--source-dir", str(bundle)), env={"PATH": "/usr/bin:/bin"})
    if verified.returncode:
        raise RuntimeError("Cannot map deployment identity: acquisition source verification failed.")
    arguments = [sys.executable, "-I", "-S", "-B", "-c",
                 "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('linux_idmap._cli',run_name='__main__')",
                 str(bundle), "--endpoint", project.docker_endpoint, "--image", image,
                 "--interpreter", "/usr/local/bin/python3", "--user-uid", "0", "--user-gid", "0",
                 "--memory-bytes", "67108864", "--pids-limit", "16", "--nano-cpus", "1000000000",
                 "--uid", str(uid), "--gid", str(gid), "--values"]
    if userns_mode is not None:
        arguments.extend(("--userns", userns_mode))
    if inverse:
        arguments.append("--inverse")
    result = subprocess.run(arguments, stdout=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"})
    if result.returncode:
        raise RuntimeError(f"Cannot map deployment identity: shared probe failed (exit {result.returncode}); inspect its diagnostic above.")
    values = result.stdout.decode("ascii").split()
    if len(values) != 2 or any(re.fullmatch(r"[0-9]+", value) is None for value in values):
        raise ValueError("Cannot map deployment identity: shared probe returned invalid values.")
    return int(values[0]), int(values[1])


def mapped_ids(project, image: str, container_uid: int, container_gid: int,
               userns_mode: str | None = None) -> tuple[int, int]:
    return _mapped_ids(project, image, container_uid, container_gid, inverse=False, userns_mode=userns_mode)


def container_ids_for_host(project, image: str, host_uid: int, host_gid: int) -> tuple[int, int]:
    return _mapped_ids(project, image, host_uid, host_gid, inverse=True)


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

