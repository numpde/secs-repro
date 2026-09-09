"""Bind the common deployment initializer to SECS's maintained configuration.

The model's examples remain authoritative. This adapter selects files, not
copied settings; changing a worker default requires no framework change.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from deployment.templates import initialize_configuration


TEMPLATES = {
    "provider.toml": Path("config/provider.toml.example"),
    "worker.toml": Path("config/worker.toml.example"),
}


def main(arguments: list[str] | None = None) -> int:
    """Initialize local config only; print its location or an actionable failure."""
    parser = argparse.ArgumentParser(description="Initialize a named SECS deployment.")
    parser.add_argument("operation", choices=("init",))
    parser.add_argument("deployment")
    options = parser.parse_args(arguments)
    try:
        destination = initialize_configuration(
            Path(__file__).resolve().parents[1], options.deployment, TEMPLATES
        )
    except (OSError, ValueError) as error:
        print(f"Deployment initialization failed: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1
    print(f"Configuration created: {destination}")
    print("Edit these files before deployment. No credentials installed or services started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
