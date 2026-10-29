"""Failure relationships remain inspectable without publishing exception payloads."""

import errno
import json
import socket
import ssl
import unittest

from secs_inference.provider.diagnostics import exception_evidence


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
        self.assertEqual(evidence["context"]["reason"], "No space left on device")
        self.assertTrue(evidence["context"]["frames"])
        self.assertNotIn("private", json.dumps(evidence))

    def test_non_os_codes_are_not_misrepresented_as_posix_failures(self):
        for error in (ssl.SSLError(1, "private"), socket.gaierror(1, "private"),
                      socket.herror(1, "private"), OSError(10 ** 100, "private")):
            with self.subTest(exception_type=type(error).__name__):
                evidence = exception_evidence(error)
                self.assertEqual(evidence["errno"], error.errno)
                self.assertNotIn("reason", evidence)
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
