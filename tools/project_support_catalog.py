#!/usr/bin/env python3
"""Project the code-owned support catalog into the bounded public-docs region."""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from secs_inference.provider.support_catalog import (  # noqa: E402
    SUPPORT_CLAIMS,
    SupportCategory,
)


START = "<!-- support-catalog:start -->"
END = "<!-- support-catalog:end -->"
_CATEGORY_TITLES = {
    SupportCategory.SUBMISSION: "Preparing a submission",
    SupportCategory.SPECTRUM: "Spectrum data",
    SupportCategory.FORMULA: "Formula evidence",
    SupportCategory.ANNOTATION: "Supplied annotations",
}


def render_support_region(claims=SUPPORT_CLAIMS) -> str:
    lines = [START]
    for category in SupportCategory:
        selected = [claim for claim in claims if claim.category is category]
        if not selected:
            continue
        lines.extend((f"  <h3>{escape(_CATEGORY_TITLES[category])}</h3>", "  <dl>"))
        for claim in selected:
            lead = claim.public_summary + "."
            if category is not SupportCategory.SUBMISSION:
                lead = "Supported input: " + lead
            detail = " ".join((lead, *claim.limits))
            lines.append(f"    <dt>{escape(claim.title)}</dt>")
            lines.append(f"    <dd>{escape(detail)}</dd>")
        lines.append("  </dl>")
    lines.append(END)
    return "\n".join(lines)


def project_support_region(document: str, claims=SUPPORT_CLAIMS) -> str:
    if document.count(START) != 1 or document.count(END) != 1:
        raise ValueError("Public documentation must contain exactly one complete support-catalog region")
    before, remainder = document.split(START, 1)
    _current, after = remainder.split(END, 1)
    return before + render_support_region(claims) + after


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "write"))
    parser.add_argument("document", type=Path)
    arguments = parser.parse_args(argv)

    current = arguments.document.read_text()
    projected = project_support_region(current)
    if arguments.operation == "check":
        if current != projected:
            print(
                f"{arguments.document} does not contain the current support-catalog projection; "
                "run the write operation and review the result.",
                file=sys.stderr,
            )
            return 1
        return 0
    arguments.document.write_text(projected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
