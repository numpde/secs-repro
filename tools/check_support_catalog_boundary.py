#!/usr/bin/env python3
"""Keep the public support catalog out of scientific admission policy."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys


CATALOG_MODULE = "secs_inference.provider.support_catalog"
ALLOWED_IMPORTERS = {Path("provider/analysis.py")}


def unexpected_catalog_importers(package_root: Path) -> list[Path]:
    unexpected = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        if relative in ALLOWED_IMPORTERS or relative == Path("provider/support_catalog.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        importing_package = ("secs_inference", *relative.parent.parts)
        if any(_imports_catalog(node, importing_package) for node in ast.walk(tree)):
            unexpected.append(relative)
    return unexpected


def _imports_catalog(node: ast.AST, importing_package: tuple[str, ...]) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == CATALOG_MODULE for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return False
    if node.level:
        retained = importing_package[:len(importing_package) - node.level + 1]
        module = ".".join((*retained, *(node.module or "").split("."))).rstrip(".")
    else:
        module = node.module or ""
    if module == CATALOG_MODULE:
        return True
    return module == "secs_inference.provider" and any(
        alias.name == "support_catalog" for alias in node.names
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    arguments = parser.parse_args(argv)
    if unexpected := unexpected_catalog_importers(arguments.package_root):
        joined = ", ".join(map(str, unexpected))
        print(
            "The public support catalog may be imported by provider/analysis.py only; "
            f"unexpected production importers: {joined}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
