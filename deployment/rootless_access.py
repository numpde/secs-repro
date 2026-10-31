"""Grant one mapped container UID the read access required by a deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid


_PERMISSIONS = {"--x", "r--", "rw-", "r-x", "rwx"}


def _mode(permissions: str) -> int:
    return sum(value for value, bit in ((4, "r"), (2, "w"), (1, "x")) if bit in permissions)


def parse_id_map(raw: bytes, label: str) -> tuple[tuple[int, int, int], ...]:
    """Parse Linux's three-column ID map without assuming its coordinate space."""
    try:
        rows = tuple(tuple(int(field) for field in line.split()) for line in raw.decode("ascii").splitlines())
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Cannot grant deployment access: host returned an invalid {label} map.") from error
    if (not rows or any(len(row) != 3 or any(value < 0 for value in row) or row[2] == 0 for row in rows)):
        raise ValueError(f"Cannot grant deployment access: host returned an invalid {label} map.")
    for index, (inside, outside, length) in enumerate(rows):
        for other_inside, other_outside, other_length in rows[index + 1:]:
            if (inside < other_inside + other_length and other_inside < inside + length
                    or outside < other_outside + other_length and other_outside < outside + length):
                raise ValueError(f"Cannot grant deployment access: host returned overlapping {label} map ranges.")
    return rows


def map_id(rows: tuple[tuple[int, int, int], ...], container_id: int, label: str) -> int:
    """Translate one container ID through a map observed from the host namespace."""
    if type(container_id) is not int or container_id < 0:
        raise ValueError(f"Cannot grant deployment access: requested {label} is invalid.")
    matches = [outside + container_id - inside for inside, outside, length in rows
               if inside <= container_id < inside + length]
    if len(matches) != 1:
        raise ValueError(f"Cannot grant deployment access: container {label} {container_id} is not mapped exactly once.")
    return matches[0]


def container_id_for_host(rows: tuple[tuple[int, int, int], ...], host_id: int, label: str) -> int:
    """Translate one host ID into the matching container ID in an observed map."""
    if type(host_id) is not int or host_id < 0:
        raise ValueError(f"Cannot select wheelhouse identity: host {label} is invalid.")
    matches = [inside + host_id - outside for inside, outside, length in rows
               if outside <= host_id < outside + length]
    if len(matches) != 1:
        raise ValueError(f"Cannot select wheelhouse identity: host {label} {host_id} is not mapped exactly once.")
    return matches[0]


def _host_user_namespace() -> None:
    """Require deployment tooling to run in the host's initial user namespace."""
    identity = ((0, 0, 4294967295),)
    try:
        uid_map = parse_id_map(Path("/proc/self/uid_map").read_bytes(), "host UID")
        gid_map = parse_id_map(Path("/proc/self/gid_map").read_bytes(), "host GID")
    except OSError as error:
        raise ValueError("Cannot grant deployment access: the host ID maps are unavailable.") from error
    if uid_map != identity or gid_map != identity:
        raise ValueError("Cannot grant deployment access: run the deployment tool in the host's initial user namespace.")


def _host_process_maps(pid: int, probe_program: str) -> tuple[tuple[tuple[int, int, int], ...], tuple[tuple[int, int, int], ...]]:
    """Tie the reported PID to this probe, then read its maps from the host."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("Cannot grant deployment access: selected engine returned an invalid probe PID.")
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        if probe_program.encode("ascii") not in command_line:
            raise ValueError("Cannot grant deployment access: host PID does not identify the selected mapping probe.")
        uid_rows = parse_id_map(Path(f"/proc/{pid}/uid_map").read_bytes(), "UID")
        gid_rows = parse_id_map(Path(f"/proc/{pid}/gid_map").read_bytes(), "GID")
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("Cannot grant deployment access: host could not inspect the selected engine's probe maps.") from error
    return uid_rows, gid_rows


def _mapping_rows(project, image: str, userns_mode: str | None = None):
    """Read both host-visible ID maps from one disposable selected-engine probe."""
    _host_user_namespace()
    token = uuid.uuid4().hex
    name = f"secs-map-{os.getpid()}-{token[:12]}"
    probe_program = f"import time; _secs_map_probe = {token!r}; time.sleep(300)"
    arguments = [
        "run", "--detach", "--name", name, "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "16", "--cpus", "1", "--memory", "64m", "--memory-swap", "64m",
        "--user", "0:0",
    ]
    if userns_mode is not None:
        if type(userns_mode) is not str or not userns_mode or any(char in userns_mode for char in "\x00\r\n"):
            raise ValueError("Cannot grant deployment access: service userns mode is invalid.")
        arguments.extend(("--userns", userns_mode))
    arguments.extend(("--entrypoint", "python", image, "-I", "-B", "-c", probe_program))
    primary_error = None
    probe_id = None
    try:
        raw_id = project.command(*arguments).decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{64}", raw_id) is None:
            raise ValueError("Cannot grant deployment access: selected engine returned an invalid probe identity.")
        probe_id = raw_id
        records = json.loads(project.command("inspect", raw_id))
        if (type(records) is not list or len(records) != 1 or type(records[0]) is not dict
                or type(records[0].get("State")) is not dict):
            raise ValueError("Cannot grant deployment access: selected engine returned an invalid probe inspection.")
        state = records[0]["State"]
        if state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 0:
            raise ValueError("Cannot grant deployment access: selected engine's mapping probe is not running.")
        pid = state["Pid"]
        uid_rows, gid_rows = _host_process_maps(pid, probe_program)
        confirmed = json.loads(project.command("inspect", raw_id))
        if (type(confirmed) is not list or len(confirmed) != 1
                or confirmed[0].get("State", {}).get("Running") is not True
                or confirmed[0].get("State", {}).get("Pid") != pid):
            raise ValueError("Cannot grant deployment access: selected engine's probe changed while its maps were read.")
        return uid_rows, gid_rows
    except BaseException as error:
        primary_error = error
        if probe_id is None:
            error.add_note(f"Mapping probe creation returned no validated ID; inspect name {name!r} without removing it by name.")
        raise
    finally:
        if probe_id is not None:
            try:
                project.command("rm", "--force", probe_id, timeout=15)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note(f"Mapping probe cleanup is unconfirmed; inspect container ID {probe_id!r} before retrying.")
                else:
                    raise ValueError(f"Cannot grant deployment access: mapping probe {probe_id!r} could not be retired.") from cleanup_error


def mapped_ids(project, image: str, container_uid: int, container_gid: int,
               userns_mode: str | None = None) -> tuple[int, int]:
    """Translate one service identity through maps observed on the selected engine."""
    uid_rows, gid_rows = _mapping_rows(project, image, userns_mode)
    return map_id(uid_rows, container_uid, "UID"), map_id(gid_rows, container_gid, "GID")


def container_ids_for_host(project, image: str, host_uid: int, host_gid: int) -> tuple[int, int]:
    """Select container IDs whose bind writes are owned by the invoking host user."""
    uid_rows, gid_rows = _mapping_rows(project, image)
    return (container_id_for_host(uid_rows, host_uid, "UID"),
            container_id_for_host(gid_rows, host_gid, "GID"))


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

