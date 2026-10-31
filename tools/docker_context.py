#!/usr/bin/env python3
"""Select one local Docker Unix socket without changing Docker's current context."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def _docker(*arguments: str) -> str:
    result = subprocess.run(
        ("/usr/bin/docker", *arguments), env={"PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ValueError("Docker context metadata could not be read.")
    return result.stdout.decode("utf-8").strip()


def _endpoint(name: str) -> str:
    names = _docker("context", "ls", "--format", "{{.Name}}").splitlines()
    if name not in names:
        raise ValueError(f"Docker context {name!r} does not exist.")
    try:
        records = json.loads(_docker("context", "inspect", name))
        endpoint = records[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Docker context {name!r} has malformed endpoint metadata.") from error
    _validate_endpoint(endpoint)
    return endpoint


def _validate_endpoint(endpoint: object) -> str:
    if type(endpoint) is not str or not endpoint.startswith("unix://"):
        raise ValueError("Only Docker contexts with a local unix:// socket are supported.")
    path = endpoint.removeprefix("unix://")
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("Docker context does not name an absolute local Unix socket.")
    return endpoint


def main() -> int:
    try:
        if sys.argv[1:] == ["--command-line-context"]:
            name = os.environ.get("DOCKER_CONTEXT", "")
            if not name:
                raise ValueError("Set DOCKER_CONTEXT=<local-context> on the make command line.")
            print(_endpoint(name))
            return 0
        if sys.argv[1:] == ["--interactive"]:
            if not sys.stdin.isatty():
                raise ValueError("Set DOCKER_CONTEXT=<local-context>; interactive context selection needs a terminal.")
            names = _docker("context", "ls", "--format", "{{.Name}}").splitlines()
            available = []
            for name in names:
                try:
                    available.append((name, _endpoint(name)))
                except ValueError:
                    continue
            if not available:
                raise ValueError("Docker has no contexts configured with local Unix sockets.")
            print("Select a local Docker context (no default):", file=sys.stderr)
            for index, (name, endpoint) in enumerate(available, 1):
                print(f"  {index}) {name} ({endpoint})", file=sys.stderr)
            while True:
                print("Choice: ", end="", file=sys.stderr, flush=True)
                choice = sys.stdin.readline()
                if not choice:
                    raise ValueError("Docker context selection was cancelled.")
                choice = choice.strip()
                if choice.isdecimal() and 1 <= int(choice) <= len(available):
                    print(available[int(choice) - 1][1])
                    return 0
                print("Choose one of the listed numbers.", file=sys.stderr)
        if sys.argv[1:] == ["--inherited-endpoint"]:
            endpoint = os.environ.get("SECS_DOCKER_ENDPOINT", "")
            if not endpoint:
                raise ValueError("No Docker endpoint was inherited from the parent make operation.")
            print(_validate_endpoint(endpoint))
            return 0
        raise ValueError("Invalid Docker context selector invocation.")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Docker context selection failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
