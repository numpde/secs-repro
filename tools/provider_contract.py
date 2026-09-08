"""Project the pinned API release from Git objects, following NMRPeak's lane.

This development-only projection supplies conformance fixtures. The running
provider does not load or authenticate these files. Authenticate the manifest's
artifact inventory before using its paths, then compare exact Git blob bytes.
"""

import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path
import subprocess


RELEASE_REF = "sha256:bb932dc51685d8403d859fdae5a887f44ecf3ca2bbed4fb1825fe16e04849b65"
SOURCE_REVISION = "05aea927599ccc941a74474b5e8c3e759168bdb2"
DESTINATION = Path(__file__).resolve().parents[1] / "contracts/upstream/nmr_api_v1"
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=True,
    )
    if len(result.stdout) > _MAX_ARTIFACT_BYTES:
        raise ValueError("API contract Git object exceeds the artifact budget")
    return result.stdout


def _blob(repository: Path, reference: str) -> bytes:
    """Bound Git blob materialization before buffering untrusted checkout data."""

    size = int(_git(repository, "cat-file", "-s", reference))
    if not 1 <= size <= _MAX_ARTIFACT_BYTES:
        raise ValueError("API contract Git object exceeds the artifact budget")
    return _git(repository, "show", reference)


def release_files(repository: Path) -> dict[str, bytes]:
    """Read an authenticated release from one fixed Git tree, not working files."""

    revision = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    prefix = f"{revision}:nmr_api/provider/contract_releases/v1/{RELEASE_REF[7:]}/"
    manifest_bytes = _blob(repository, prefix + "manifest.json")
    manifest = json.loads(manifest_bytes)
    material = {"artifacts": manifest["artifacts"], "contract_id": manifest["contract_id"]}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if (
        "sha256:" + sha256(encoded).hexdigest() != RELEASE_REF
        or manifest["release_ref"] != RELEASE_REF
        or manifest["source_revision"] != SOURCE_REVISION
    ):
        raise ValueError("API contract manifest does not identify the selected release")
    files = {"manifest.json": manifest_bytes}
    for artifact in manifest["artifacts"]:
        path = artifact["path"]
        raw = _blob(repository, prefix + path)
        digest = sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        if len(raw) != artifact["byte_length"] or digest != artifact["git_blob_sha1"]:
            raise ValueError(f"API contract artifact differs from its manifest: {path}")
        files[path] = raw
    return files


def project(repository: Path, *, write: bool) -> None:
    """Check exact projection bytes, or explicitly write the reviewed inventory."""

    files = release_files(repository)
    for parent in (DESTINATION.parent.parent, DESTINATION.parent, DESTINATION):
        if parent.is_symlink():
            raise ValueError(f"Contract projection directory is a symlink: {parent}")
    existing = set()
    for path in DESTINATION.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Contract projection contains a non-regular entry: {path}")
        if path.is_file():
            existing.add(path.relative_to(DESTINATION).as_posix())
    if existing - files.keys():
        raise ValueError("Contract projection contains files outside the selected release")
    for relative, raw in files.items():
        path = DESTINATION / relative
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        elif not path.is_file() or path.read_bytes() != raw:
            raise ValueError(f"Contract projection differs from the release: {relative}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "write"))
    parser.add_argument("api_repository", type=Path)
    args = parser.parse_args()
    project(args.api_repository, write=args.operation == "write")
