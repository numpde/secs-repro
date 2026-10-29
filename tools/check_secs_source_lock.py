#!/usr/bin/env python3
"""Check the public SECS source lock against the repository's indexed submodule."""

from __future__ import annotations

import configparser
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "contracts/upstream/secs_source.json"
FIELDS = {"schema_id", "submodule_path", "repository", "revision"}


def main() -> int:
    document = json.loads(LOCK.read_text())
    if set(document) != FIELDS or document["schema_id"] != "secs.source-lock.v1":
        raise ValueError(f"{LOCK.relative_to(ROOT)} has an unknown shape or schema")
    if not re.fullmatch(r"[0-9a-f]{40}", document["revision"]):
        raise ValueError("SECS source revision must be a full lowercase Git object ID")
    if not document["repository"].startswith("https://"):
        raise ValueError("SECS source repository must be publicly readable over HTTPS")

    modules = configparser.ConfigParser()
    modules.read(ROOT / ".gitmodules")
    section = "submodule \"secs\""
    if section not in modules:
        raise ValueError(".gitmodules has no secs submodule")
    if modules[section].get("path") != document["submodule_path"]:
        raise ValueError("SECS source lock path differs from .gitmodules")
    if modules[section].get("url") != document["repository"]:
        raise ValueError("SECS source lock repository differs from .gitmodules")

    indexed = subprocess.run(
        ["git", "ls-files", "--stage", "--", document["submodule_path"]],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().split()
    if len(indexed) != 4 or indexed[0] != "160000":
        raise ValueError("SECS source path is not one indexed Git submodule")
    if indexed[1] != document["revision"]:
        raise ValueError("SECS source lock revision differs from the indexed submodule")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
