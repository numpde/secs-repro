"""Make hash-pinned Torch wheel requirements installable from a local wheelhouse."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


TORCH_URL = re.compile(r"^torch @ (?P<url>\S+)#sha256=[0-9a-f]{64}$")


def normalize(line: str) -> str:
    requirement = line.rstrip()
    if requirement.endswith("\\"):
        requirement = requirement[:-1].rstrip()
    match = TORCH_URL.match(requirement)
    if match is None:
        return line

    filename = unquote(Path(urlsplit(match["url"]).path).name)
    version = filename.removeprefix("torch-").split("-", 1)[0]
    return f"torch=={version} \\\n"


def main(source: Path, destination: Path) -> None:
    with source.open() as lines, destination.open("w") as output:
        for line_number, line in enumerate(lines):
            output.write(normalize(line))
            if line_number == 0:
                output.write("# Direct Torch wheel references were normalized for offline installation.\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
