"""Failure relationships remain inspectable without publishing exception payloads."""

import errno
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import WorkDeadlineExceeded
from secs_inference.provider.upload_download import UploadUnavailable
from test_execution import ACTIVE


class DiagnosticTests(unittest.TestCase):
    def test_suppressed_context_survives_without_text_notes_or_arbitrary_details(self):
        try:
            try:
                error = OSError(errno.ENOSPC, "private-message", "/private-input")
                error.add_note("private-note")
                error.diagnostic = {"private": "third-party-value"}
                raise error
            except OSError:
                raise RuntimeError("private-wrapper") from None
        except RuntimeError as failure:
            evidence = exception_evidence(failure)
        self.assertEqual(evidence["exception_type"], "RuntimeError")
        self.assertEqual(evidence["context"]["exception_type"], "OSError")
        self.assertEqual(evidence["context"]["errno"], errno.ENOSPC)
        self.assertTrue(evidence["context"]["frames"])
        self.assertNotIn("private", json.dumps(evidence))

    def test_explicit_cause_is_not_conflated_with_unrelated_context(self):
        error = RuntimeError()
        error.__cause__ = ValueError()
        error.__context__ = KeyError()
        evidence = exception_evidence(error)
        self.assertEqual(evidence["cause"]["exception_type"], "ValueError")
        self.assertNotIn("context", evidence)

    def test_repeated_exceptions_and_wide_groups_are_bounded(self):
        repeated = ValueError("private")
        repeated.__context__ = repeated
        evidence = exception_evidence(ExceptionGroup("private", [repeated, repeated]))
        self.assertEqual(evidence["exceptions"][0]["context"]["exception_reference"], evidence["exceptions"][0]["exception_id"])
        self.assertEqual(evidence["exceptions"][1]["exception_reference"], evidence["exceptions"][0]["exception_id"])
        shared = ValueError("private")
        for members in ([ValueError("private") for _ in range(1000)], [shared] * 1000):
            with self.subTest(shared=members[0] is members[1]):
                evidence = exception_evidence(ExceptionGroup("private", members))
                self.assertEqual(len(evidence["exceptions"]), 63)
                self.assertEqual(evidence["omitted_exception_count"], 937)
                self.assertNotIn("private", json.dumps(evidence))

    def test_acquisition_deadline_retains_the_owned_transport_cause(self):
        try:
            try:
                raise UploadUnavailable("the connection was reset", diagnostic={
                    "phase": "receiving the selected Upload's bytes", "errno": errno.ECONNRESET,
                })
            except UploadUnavailable as cause:
                raise WorkDeadlineExceeded("Analysis stopped while obtaining the selected Upload: the input acquisition deadline elapsed") from cause
        except WorkDeadlineExceeded as failure:
            with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
                journal.diagnose(ACTIVE, failure)
                evidence = json.loads(next(journal.directory.glob("*.diagnostic.json")).read_bytes())
        self.assertEqual(evidence["execution_attempt_ref"], ACTIVE.execution_attempt_ref)
        self.assertEqual(evidence["cause"]["upload"]["errno"], errno.ECONNRESET)
        self.assertIn("receiving", evidence["cause"]["upload"]["phase"])

    def test_attempt_diagnostics_do_not_adopt_third_party_diagnostic_attributes(self):
        error = ValueError("private-message")
        error.diagnostic = {"private": "third-party-value"}
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            journal.diagnose(ACTIVE, error)
            evidence = json.loads(next(journal.directory.glob("*.diagnostic.json")).read_bytes())
        self.assertNotIn("private", json.dumps(evidence))
        self.assertNotIn("worker", evidence)
