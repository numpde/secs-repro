"""A parser receives only verified bytes; credentials stay at the store."""

from base64 import b64encode
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.upload_download import (
    UploadDownloadError, UploadStore, UploadUnavailable, downloaded_upload,
)
from secs_inference.provider.socket_deadline import socket_deadline
import socket
from test_http import _tls_server, _write_test_certificates


class UploadDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.certificates = Path(cls.directory.name)
        _write_test_certificates(cls.certificates)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def _store(self, port):
        return UploadStore(f"https://localhost:{port}", 1024, ca_file=self.certificates / "ca.pem")

    def _grant(self, port, body=b"data"):
        ref = "upload:sha256:" + "a" * 64
        return UploadReadCapability(
            ref, len(body), "sha256:" + sha256(body).hexdigest(),
            datetime(2030, 1, 1, tzinfo=UTC),
            f"https://localhost:{port}/upload/v1/uploads/{ref}/bytes", "private-bearer",
        )

    def _headers(self, body=b"data"):
        return {
            "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
            "Content-Digest": "sha-256=:" + b64encode(sha256(body).digest()).decode() + ":",
        }

    def test_verified_file_lives_with_its_context_and_get_has_only_store_authority(self):
        with TemporaryDirectory() as directory, _tls_server(
            self.certificates, response_headers=self._headers(), response_body=b"data",
        ) as server:
            grant = self._grant(server.port)
            with downloaded_upload(
                store=self._store(server.port), capability=grant,
                directory=Path(directory), deadline=monotonic() + 2,
            ) as path:
                self.assertEqual(path.read_bytes(), b"data")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.exists())
            request = server.requests[0]
            self.assertEqual(request["requestline"], f"GET {grant.download_url.split(str(server.port))[1]} HTTP/1.1")
            self.assertEqual(request["headers"], {
                "Host": f"localhost:{server.port}", "Authorization": "Bearer private-bearer",
            })

    def test_untrusted_destination_and_header_are_rejected_before_network(self):
        with TemporaryDirectory() as directory, _tls_server(self.certificates) as server:
            grant = self._grant(server.port)
            for change in (
                {"download_url": grant.download_url + "?token=private"},
                {"download_url": "https://another.example/"},
                {"capability": "private\r\nInjected: yes"},
            ):
                with self.subTest(change=change), self.assertRaises(UploadDownloadError) as caught:
                    with downloaded_upload(
                        store=self._store(server.port), capability=replace(grant, **change),
                        directory=Path(directory), deadline=monotonic() + 2,
                    ):
                        self.fail("Unverified source was exposed")
                self.assertNotIn("private", str(caught.exception))
            self.assertEqual(server.requests, [])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_corruption_truncation_and_header_conflicts_leave_no_source(self):
        for body, headers, length in (
            (b"evil", self._headers(), 4),
            (b"dat", self._headers(), 4),
            (b"data", self._headers() | {"Content-Encoding": "gzip"}, 4),
            (b"data", self._headers() | {"Content-Digest": "wrong"}, 4),
            (b"data", self._headers(), 5),
        ):
            with self.subTest(body=body, headers=headers, length=length), TemporaryDirectory() as directory:
                with _tls_server(self.certificates, response_body=body, response_headers=headers, declared_response_length=length) as server:
                    with self.assertRaises(UploadDownloadError):
                        with downloaded_upload(
                            store=self._store(server.port), capability=self._grant(server.port),
                            directory=Path(directory), deadline=monotonic() + 2,
                        ):
                            self.fail("Unverified source was exposed")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_redirect_is_not_followed_and_status_is_retained(self):
        with TemporaryDirectory() as directory, _tls_server(self.certificates, status=302) as server:
            with self.assertRaises(UploadDownloadError) as caught:
                with downloaded_upload(
                    store=self._store(server.port), capability=self._grant(server.port),
                    directory=Path(directory), deadline=monotonic() + 2,
                ):
                    self.fail("Redirect was accepted")
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(len(server.requests), 1)

    def test_deadline_interrupts_dripping_headers_and_body(self):
        for drip in ({"header_drip_seconds": 0.02}, {"drip_seconds": 0.1}):
            with self.subTest(drip=drip), TemporaryDirectory() as directory:
                with _tls_server(self.certificates, response_headers=self._headers(), response_body=b"data", **drip) as server:
                    started = monotonic()
                    with self.assertRaises(UploadUnavailable):
                        with downloaded_upload(
                            store=self._store(server.port), capability=self._grant(server.port),
                            directory=Path(directory), deadline=started + 0.15,
                        ):
                            self.fail("Deadline was ignored")
                    self.assertLess(monotonic() - started, 0.8)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_expired_deadline_rejects_even_a_non_io_completion(self):
        left, right = socket.socketpair()
        try:
            with self.assertRaises(TimeoutError):
                with socket_deadline(left, monotonic() + 0.01):
                    sleep(0.03)
        finally:
            left.close()
            right.close()

    def test_certificate_and_disk_failures_are_not_transient_network_failures(self):
        with TemporaryDirectory() as directory, _tls_server(
            self.certificates, response_headers=self._headers(), response_body=b"data",
        ) as server:
            untrusted = UploadStore(f"https://localhost:{server.port}", 1024)
            with self.assertRaises(UploadDownloadError) as caught:
                with downloaded_upload(store=untrusted, capability=self._grant(server.port), directory=Path(directory), deadline=monotonic() + 2):
                    self.fail("Untrusted store was accepted")
            self.assertNotIsInstance(caught.exception, UploadUnavailable)
            # File writes are the local storage boundary, independent of TLS.
            with patch("tempfile._TemporaryFileWrapper.write", create=True, side_effect=OSError("disk full")):
                with self.assertRaises(UploadDownloadError) as caught:
                    with downloaded_upload(store=self._store(server.port), capability=self._grant(server.port), directory=Path(directory), deadline=monotonic() + 2):
                        self.fail("Failed write was accepted")
            self.assertNotIsInstance(caught.exception, UploadUnavailable)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_store_settings_bound_actual_resource_effects(self):
        for length, timeout in ((float("nan"), 1), (True, 1), (1024, float("inf")), (1024, float("nan"))):
            with self.subTest(length=length, timeout=timeout), self.assertRaises(ValueError):
                UploadStore("https://localhost", length, timeout)
        with self.assertRaises(ValueError):
            UploadStore("https://localhost:0", 1024)
