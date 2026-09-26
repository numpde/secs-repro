#!/usr/bin/env python3
"""Run maintained input tests and check their semantic support evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest

from support_evidence import test_evidence


def _tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def declared_evidence(suite) -> set[str]:
    owners = {}
    for test in _tests(suite):
        for requirement in test_evidence(test):
            if requirement in owners:
                raise ValueError(
                    f"Qualification evidence {requirement} is declared by more than one test"
                )
            owners[requirement] = test.id()
    return set(owners)


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_evidence = set()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_evidence.update(test_evidence(test))


def compare_evidence(*, required: set[str], declared: set[str], passed: set[str]) -> None:
    problems = []
    if missing := required - declared:
        problems.append("missing declaration: " + ", ".join(sorted(missing)))
    if stale := declared - required:
        problems.append("declared but not required: " + ", ".join(sorted(stale)))
    if unpassed := required - passed:
        problems.append("required evidence did not pass: " + ", ".join(sorted(unpassed)))
    if problems:
        raise ValueError("; ".join(problems))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--start", type=Path, required=True)
    arguments = parser.parse_args(argv)

    from secs_inference.provider.support_catalog import SUPPORT_CLAIMS

    required = {
        evidence_id
        for claim in SUPPORT_CLAIMS
        for evidence_id in claim.required_evidence
    }
    suite = unittest.defaultTestLoader.discover(
        str(arguments.start), pattern="test_*.py", top_level_dir=str(arguments.tests)
    )
    try:
        declared = declared_evidence(suite)
    except ValueError as error:
        print(f"Support evidence is invalid: {error}", file=sys.stderr)
        return 1

    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    if not result.wasSuccessful():
        print("Support coverage was not established because the maintained suite failed.", file=sys.stderr)
        return 1
    try:
        compare_evidence(required=required, declared=declared, passed=result.passed_evidence)
    except ValueError as error:
        print(f"Support coverage is incomplete: {error}", file=sys.stderr)
        return 1
    print(f"Support coverage passed for {len(required)} semantic evidence requirements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
