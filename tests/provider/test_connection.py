"""Resolved-address failures stay visible without changing successful fallback."""

import errno
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.connection import _connect_tcp
from secs_inference.provider.network_errors import AddressFailure, ConnectionFailed, network_failure_evidence, network_failure_reason
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.api import ProviderApi
from secs_inference.provider.http import HttpsEndpoint
from secs_inference.provider.job_api import JobApi
from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.upload_download import UploadStore, download_upload
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_execution import FakeApi, START


ADDRESSES = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
]


class ConnectionTests(unittest.TestCase):
    def test_api_and_upload_keep_all_connection_failures_at_their_own_boundaries(self):
        for boundary in ("api", "upload"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory)
                sources = root / "sources"
                sources.mkdir()
                transport = ProviderApi(HttpsEndpoint("https://api.test", "web", 1, 1), START.provider_ref,
                    "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
                store = UploadStore("https://store.test", 100)
                capability = UploadReadCapability("upload:sha256:" + "a" * 64, 1, "sha256:" + "b" * 64,
                    datetime(2099, 1, 1, tzinfo=UTC), "https://store.test/upload/v1/uploads/upload:sha256:" + "a" * 64 + "/bytes", "private-bearer")
                analyse = JobApi(transport).specification if boundary == "api" else lambda _: download_upload(
                    store=store, capability=capability, directory=sources, deadline=monotonic() + 120)
                sockets = [Mock(), Mock()]
                sockets[0].connect.side_effect = TimeoutError("private-request")
                sockets[1].connect.side_effect = OSError(errno.ENETUNREACH, "private-host")
                with AttemptStore(root / "journal") as journal, patch(
                        "secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES), patch(
                        "secs_inference.provider.connection.socket.socket", side_effect=sockets):
                    api = FakeApi()
                    ExecutionLoop(api, journal, analyse, journal.diagnose).step()
                message = json.loads(api.calls[-1])["failure_message"]
                self.assertIn("IPv4: the connection timed out", message)
                self.assertIn("IPv6: Network is unreachable", message)
                evidence = json.loads(next((root / "journal").glob("*.diagnostic.json")).read_bytes())
                self.assertEqual(len(evidence[boundary]["connection_attempts"]), 2)
                if boundary == "api":
                    self.assertEqual(evidence[boundary]["delivery"], "not_sent")
                    self.assertIn("request was not sent", message)
                else:
                    self.assertIn("no verified file", message)
                    self.assertEqual(list(sources.iterdir()), [])
                self.assertNotIn("private-", message + json.dumps(evidence))

    def test_failed_socket_cleanup_preserves_both_causes_and_stops_fallback(self):
        transport = Mock()
        transport.connect.side_effect = ConnectionRefusedError("private-connect")
        transport.close.side_effect = OSError(errno.EIO, "private-cleanup")
        with patch("secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES), patch(
                "secs_inference.provider.connection.socket.socket", return_value=transport) as sockets:
            with self.assertRaises(ConnectionFailed) as caught:
                _connect_tcp(("model.test", 443), 10)
        sockets.assert_called_once()
        evidence = network_failure_evidence(caught.exception)
        self.assertEqual(evidence["unattempted_address_count"], 1)
        self.assertEqual(evidence["connection_attempts"][0]["socket_cleanup"]["errno"], errno.EIO)
        reason = network_failure_reason(caught.exception)
        self.assertIn("connection was refused", reason)
        self.assertIn("socket cleanup also failed", reason)
        self.assertNotIn("all 2", reason)
        self.assertNotIn("private-", reason + json.dumps(evidence))

    def test_private_ipv6_evidence_keeps_scope_and_flow_information(self):
        failure = ConnectionFailed((AddressFailure(socket.AF_INET6, ("fe80::1", 443, 12, 7), OSError(errno.ENETUNREACH, "private")),))
        attempt = network_failure_evidence(failure)["connection_attempts"][0]
        self.assertEqual((attempt["address"], attempt["port"], attempt["flow_info"], attempt["scope_id"]), ("fe80::1", 443, 12, 7))

    def test_expired_interpretation_deadline_keeps_each_connection_cause(self):
        endpoint = ChatEndpoint("https://model.test/chat", "test-model", "private-key")
        sockets = [Mock(), Mock()]
        sockets[0].connect.side_effect = TimeoutError()
        sockets[1].connect.side_effect = OSError(errno.ENETUNREACH, "private-host")
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            api = FakeApi()
            with patch("secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES), patch(
                    "secs_inference.provider.connection.socket.socket", side_effect=sockets), patch(
                    "secs_inference.provider.chat.monotonic", side_effect=[0, 121]):
                ExecutionLoop(api, journal, lambda _: endpoint.complete([], [], deadline=120), journal.diagnose).step()
            message = json.loads(api.calls[-1])["failure_message"]
            self.assertIn("interpretation deadline elapsed", message)
            self.assertIn("IPv4: the connection timed out", message)
            self.assertIn("IPv6: Network is unreachable", message)
            self.assertIn("this request was not sent", message)

    def test_mixed_address_failures_reach_attempt_message_and_private_evidence(self):
        endpoint = ChatEndpoint("https://model.test/chat", "test-model", "private-key")
        sockets = [Mock(), Mock()]
        sockets[0].connect.side_effect = TimeoutError("private-request")
        sockets[1].connect.side_effect = OSError(errno.ENETUNREACH, "private-host")
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            api = FakeApi()
            with patch("secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES), patch(
                    "secs_inference.provider.connection.socket.socket", side_effect=sockets):
                ExecutionLoop(api, journal, lambda _: endpoint.complete([], [], deadline=monotonic() + 120), journal.diagnose).step()
            message = json.loads(api.calls[-1])["failure_message"]
            self.assertIn("IPv4: the connection timed out", message)
            self.assertIn("IPv6: Network is unreachable", message)
            self.assertIn("this request was not sent", message)
            evidence = json.loads(next(journal.directory.glob("*.diagnostic.json")).read_bytes())
            attempts = evidence["interpreter"]["connection_attempts"]
            self.assertEqual([item["address"] for item in attempts], ["192.0.2.1", "2001:db8::1"])
            self.assertEqual([item["errno"] for item in attempts], [None, errno.ENETUNREACH])
            self.assertNotIn("private-", message + json.dumps(evidence))
        for transport in sockets:
            transport.close.assert_called_once()

    def test_failed_first_address_does_not_prevent_successful_fallback(self):
        sockets = [Mock(), Mock()]
        sockets[0].connect.side_effect = ConnectionRefusedError()
        with patch("secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES), patch(
                "secs_inference.provider.connection.socket.socket", side_effect=sockets):
            result = _connect_tcp(("model.test", 443), 10)
        self.assertIs(result, sockets[1])
        sockets[0].close.assert_called_once()
        sockets[1].close.assert_not_called()
        sockets[1].connect.assert_called_once_with(ADDRESSES[1][4])
        for transport in sockets:
            transport.settimeout.assert_called_once_with(10)

    def test_dns_failure_is_not_misreported_as_failed_address_connections(self):
        failure = socket.gaierror(socket.EAI_NONAME, "private-host")
        with patch("secs_inference.provider.connection.socket.getaddrinfo", side_effect=failure):
            with self.assertRaises(socket.gaierror) as caught:
                _connect_tcp(("model.test", 443), 10)
        self.assertIs(caught.exception, failure)
        self.assertNotIn("private-host", network_failure_reason(failure))

    def test_repeated_address_causes_are_compact_publicly_and_complete_privately(self):
        transport = Mock()
        transport.connect.side_effect = ConnectionRefusedError("private-details")
        with patch("secs_inference.provider.connection.socket.getaddrinfo", return_value=ADDRESSES * 30), patch(
                "secs_inference.provider.connection.socket.socket", return_value=transport):
            with self.assertRaises(ConnectionFailed) as caught:
                _connect_tcp(("model.test", 443), 10)
        self.assertEqual(len(network_failure_evidence(caught.exception)["connection_attempts"]), 60)
        self.assertLess(len(network_failure_reason(caught.exception)), 250)


if __name__ == "__main__":
    unittest.main()
