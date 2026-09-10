"""Closing an HTTP resource cannot invent a failed send or destroy its cause."""

from base64 import b64encode
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from email.message import Message
import errno
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.chat import ChatEndpoint, InterpreterError
from secs_inference.provider.http import HttpsEndpoint, HttpResponse, RequestUnavailable, RequestDelivery, send_hello_request
from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.upload_download import UploadStore, UploadDownloadError, UploadUnavailable, download_upload
from test_http import _signed_hello


def exchange(body, headers):
    response = Mock(status=200, length=0)
    response.headers = Message()
    for name, value in headers.items():
        response.headers[name] = value
    response.getheaders.return_value = list(headers.items())
    response.read1.side_effect = [body, b""]
    response.isclosed.return_value = False
    response.close.side_effect = OSError(errno.EIO, "private-response-close")
    connection = Mock()
    connection.getresponse.return_value = response
    connection.close.side_effect = OSError(errno.EBADF, "private-connection-close")
    return connection, response


class HttpCleanupTests(unittest.TestCase):
    def test_deadline_at_context_exit_preserves_received_response_correlation(self):
        @contextmanager
        def elapsed_deadline(*_args):
            yield
            raise TimeoutError("deadline elapsed")

        endpoint = HttpsEndpoint("https://api.test", "web", 1, 1)
        connection = Mock()
        with patch("secs_inference.provider.http.https_connection", return_value=connection), \
             patch("secs_inference.provider.http.socket_deadline", elapsed_deadline), \
             patch("secs_inference.provider.http._exchange", return_value=HttpResponse(503, "request-test", b"{}")):
            outcome = send_hello_request(endpoint=endpoint, request=_signed_hello(endpoint.authority, b"{}"))
        self.assertIsInstance(outcome, RequestUnavailable)
        self.assertEqual(outcome.delivery, RequestDelivery.RESPONSE_RECEIVED)
        self.assertEqual(outcome.status, 503)
        self.assertEqual(outcome.request_id, "request-test")
        self.assertIsInstance(outcome.cause, TimeoutError)
        connection.close.assert_called_once()

    def assert_cleanup_evidence(self, captured, operation):
        self.assertEqual(len(captured.output), 2)
        for line, role, number in zip(captured.output, ("response", "connection"), (errno.EIO, errno.EBADF)):
            self.assertIn(operation, line)
            self.assertIn(f"HTTP {role}", line)
            self.assertIn(f'"errno": {number}', line)
            self.assertNotIn("private-", line)

    def test_model_success_and_failure_survive_both_close_failures(self):
        message = {"role": "assistant", "content": "accepted"}
        raw = json.dumps({"choices": [{"message": message}]}).encode()
        endpoint = ChatEndpoint("https://model.test/chat", "chosen-model", "private-key")
        for broken in (False, True):
            with self.subTest(broken=broken):
                connection, response = exchange(raw, {})
                if broken:
                    response.read1.side_effect = ConnectionResetError(errno.ECONNRESET, "private-read")
                with patch("secs_inference.provider.chat.https_connection", return_value=connection), \
                     patch("secs_inference.provider.chat.socket_deadline", return_value=nullcontext()), \
                     self.assertLogs("secs_inference.provider.http_cleanup", level="ERROR") as captured:
                    if broken:
                        with self.assertRaises(InterpreterError) as caught:
                            endpoint.complete([], [], deadline=monotonic() + 5)
                        self.assertEqual(caught.exception.diagnostic["errno"], errno.ECONNRESET)
                        self.assertIn("connection was reset", str(caught.exception))
                    else:
                        self.assertEqual(endpoint.complete([], [], deadline=monotonic() + 5), message)
                connection.request.assert_called_once()
                self.assert_cleanup_evidence(captured, "chosen-model")

    def test_api_receipt_and_uncertain_delivery_survive_both_close_failures(self):
        endpoint = HttpsEndpoint("https://api.test", "web", 1, 1)
        raw = b"{}"
        for broken in (False, True):
            with self.subTest(broken=broken):
                connection, response = exchange(raw, {
                    "Content-Type": "application/json", "Cache-Control": "no-store",
                    "Nmr-Api-Topology": "web", "Content-Length": str(len(raw)),
                })
                if broken:
                    response.read1.side_effect = ConnectionResetError(errno.ECONNRESET, "private-read")
                with patch("secs_inference.provider.http.https_connection", return_value=connection), \
                     patch("secs_inference.provider.http.socket_deadline", return_value=nullcontext()), \
                     self.assertLogs("secs_inference.provider.http_cleanup", level="ERROR") as captured:
                    outcome = send_hello_request(endpoint=endpoint, request=_signed_hello(endpoint.authority, b"{}"))
                if broken:
                    self.assertIsInstance(outcome, RequestUnavailable)
                    self.assertEqual(outcome.delivery, RequestDelivery.RESPONSE_RECEIVED)
                    self.assertEqual(outcome.cause.errno, errno.ECONNRESET)
                    self.assertEqual(outcome.status, 200)
                else:
                    self.assertEqual(outcome, HttpResponse(200, None, raw))
                connection.endheaders.assert_called_once()
                self.assert_cleanup_evidence(captured, "publish hello")

    def test_verified_upload_survives_close_failure_but_broken_transfer_leaves_no_file(self):
        raw = b"verified input"
        upload_ref = "upload:sha256:" + "a" * 64
        digest = sha256(raw).digest()
        store = UploadStore("https://store.test", 100)
        capability = UploadReadCapability(upload_ref, len(raw), "sha256:" + digest.hex(), datetime(2099, 1, 1, tzinfo=UTC),
            f"https://store.test/upload/v1/uploads/{upload_ref}/bytes", "private-bearer")
        for outcome in ("success", "broken", "rejected"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                connection, response = exchange(raw, {
                    "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
                    "Content-Length": str(len(raw)), "Content-Digest": "sha-256=:" + b64encode(digest).decode() + ":",
                })
                if outcome == "broken":
                    response.read1.side_effect = ConnectionResetError(errno.ECONNRESET, "private-read")
                elif outcome == "rejected":
                    response.status = 403
                with patch("secs_inference.provider.upload_download.https_connection", return_value=connection), \
                     patch("secs_inference.provider.upload_download.socket_deadline", return_value=nullcontext()), \
                     self.assertLogs("secs_inference.provider.http_cleanup", level="ERROR") as captured:
                    if outcome != "success":
                        with self.assertRaises(UploadDownloadError) as caught:
                            download_upload(store=store, capability=capability, directory=Path(directory), deadline=monotonic() + 5)
                        if outcome == "broken":
                            self.assertIsInstance(caught.exception, UploadUnavailable)
                            self.assertEqual(caught.exception.diagnostic["errno"], errno.ECONNRESET)
                        else:
                            self.assertNotIsInstance(caught.exception, UploadUnavailable)
                            self.assertEqual(caught.exception.status, 403)
                        self.assertEqual(list(Path(directory).iterdir()), [])
                    else:
                        path = download_upload(store=store, capability=capability, directory=Path(directory), deadline=monotonic() + 5)
                        self.assertEqual(path.read_bytes(), raw)
                connection.endheaders.assert_called_once()
                self.assert_cleanup_evidence(captured, upload_ref)
