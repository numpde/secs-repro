"""Network diagnostics describe observed failures, never exception-owned text."""

import errno
import http.client
import json
import socket
import ssl
import unittest

from secs_inference.provider.network_errors import network_failure_evidence, network_failure_reason
from secs_inference.provider.diagnostics import exception_evidence


class NetworkErrorTests(unittest.TestCase):
    def test_native_tls_identifiers_survive_without_verification_text(self):
        error = ssl.SSLCertVerificationError(1, "private certificate")
        error.library = "SSL"
        error.reason = "CERTIFICATE_VERIFY_FAILED"
        error.verify_code = 20
        error.verify_message = "private hostname and certificate detail"
        for evidence in (network_failure_evidence(error), exception_evidence(error)):
            self.assertEqual(evidence["tls_library"], "SSL")
            self.assertEqual(evidence["tls_reason"], "CERTIFICATE_VERIFY_FAILED")
            self.assertEqual(evidence["tls_verify_code"], 20)
            self.assertNotIn("private", json.dumps(evidence))
        empty = network_failure_evidence(ssl.SSLError(1, "private"))
        self.assertNotIn("tls_library", empty)
        self.assertNotIn("tls_reason", empty)
        self.assertNotIn("tls_verify_code", empty)
        for invalid_code in ("private errno", object()):
            error.errno = invalid_code
            for evidence in (network_failure_evidence(error), exception_evidence(error)):
                self.assertIsNone(evidence["errno"])
                self.assertNotIn("private", json.dumps(evidence))

    def test_known_causes_have_plain_reasons_without_remote_or_secret_text(self):
        cases = (
            (socket.gaierror(-2, "private hostname"), "address could not be resolved"),
            (ssl.SSLCertVerificationError(1, "private certificate"), "certificate could not be verified"),
            (ssl.SSLError(1, "private TLS bytes"), "encrypted connection failed"),
            (TimeoutError("private URL"), "timed out"),
            (ConnectionRefusedError("private endpoint"), "connection was refused"),
            (ConnectionResetError("private endpoint"), "connection was reset"),
            (ConnectionAbortedError("private endpoint"), "connection was aborted"),
            (BrokenPipeError("private bytes"), "connection closed"),
            (http.client.RemoteDisconnected("private status"), "closed the connection without a reply"),
            (http.client.IncompleteRead(b"private response", 42), "declared bytes"),
            (EOFError("private incomplete reply"), "before completion"),
            (http.client.BadStatusLine("private HTTP bytes"), "unreadable HTTP response"),
            (http.client.CannotSendRequest("private client state"), "HTTP exchange could not be completed"),
            (RuntimeError("private missing socket detail"), "connection failed"),
            (OSError(errno.EACCES, "private path"), "Permission denied"),
            (OSError("private unclassified error"), "no specific cause was available"),
        )
        for error, evidence in cases:
            with self.subTest(kind=type(error).__name__):
                reason = network_failure_reason(error)
                self.assertIn(evidence, reason)
                self.assertNotIn("private", reason)
