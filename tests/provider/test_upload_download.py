"""A parser receives only verified bytes; credentials stay at the store."""

from base64 import b64encode
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import errno
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.upload_download import (
    UploadDownloadError, UploadStore, UploadUnavailable, download_upload,
)
from secs_inference.provider.socket_deadline import socket_deadline
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.analysis_run import AttemptSources
from secs_inference.provider.execution import ExecutionLoop
from test_execution import FakeApi
import socket
from test_http import _tls_server, _write_test_certificates


@contextmanager
def downloaded_upload(**arguments):
    """This fixture, like the execution owner, releases successfully acquired files."""
    path = download_upload(**arguments)
    try:
        yield path
    finally:
        path.unlink()


class UploadDownloadTests(unittest.TestCase):
    def test_transfer_failures_report_phase_and_do_not_publish_unverified_files(self):
        phases = (
            ("connect", "connecting to the upload store"),
            ("endheaders", "requesting the selected Upload's bytes"),
            ("getresponse", "waiting for the upload store to reply"),
            ("copy", "receiving the selected Upload's bytes"),
        )
        for failing_call, phase in phases:
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                sources = root / "sources"
                sources.mkdir()
                connection = Mock()
                failure = ConnectionResetError("private bearer and URL")
                if failing_call != "copy":
                    getattr(connection, failing_call).side_effect = failure
                capability = UploadReadCapability("upload:sha256:" + "a" * 64, 1, "sha256:" + "b" * 64,
                    datetime(2099, 1, 1, tzinfo=UTC), "https://store.test/upload/v1/uploads/upload:sha256:" + "a" * 64 + "/bytes", "private-bearer")
                with (AttemptStore(root / "journal") as journal,
                      patch("secs_inference.provider.upload_download.http.client.HTTPSConnection", return_value=connection),
                      patch("secs_inference.provider.upload_download.socket_deadline", return_value=nullcontext()),
                      patch("secs_inference.provider.upload_download._copy_verified", side_effect=failure)):
                    api = FakeApi()
                    ExecutionLoop(api, journal, lambda _: download_upload(store=UploadStore("https://store.test", 100),
                        capability=capability, directory=sources, deadline=monotonic() + 10), journal.diagnose).step()
                result = json.loads(api.calls[-1])
                self.assertEqual(result["failure_code"], "input_access_failed")
                self.assertIn(phase, result["failure_message"])
                self.assertIn("connection was reset", result["failure_message"])
                self.assertIn("no verified file", result["failure_message"])
                private = json.loads(next((root / "journal").glob("*.diagnostic.json")).read_bytes())
                self.assertEqual(private["upload"]["phase"], phase)
                self.assertNotIn("worker", private)
                self.assertNotIn("private bearer", json.dumps(private))
                self.assertNotIn("private-bearer", result["failure_message"])
                self.assertEqual(list(sources.iterdir()), [])

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

    def test_verified_private_file_and_get_have_only_the_granted_authority(self):
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

    def test_failed_transfer_cleanup_is_visible_even_if_acquisition_retries(self):
        with TemporaryDirectory() as directory:
            failure = UploadUnavailable("transfer interrupted")
            with self.assertLogs("secs_inference.provider.upload_download", level="ERROR") as logged:
                with patch("secs_inference.provider.upload_download._transfer", side_effect=failure), patch(
                        "secs_inference.provider.upload_download.Path.unlink", side_effect=PermissionError("private-bearer")):
                    with self.assertRaises(UploadUnavailable) as caught:
                        download_upload(store=self._store(443), capability=self._grant(443),
                                        directory=Path(directory), deadline=monotonic() + 2)
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertIn("may remain in the Attempt workspace", " ".join(logged.output))
            self.assertNotIn("private-bearer", " ".join(logged.output))

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
            with patch("tempfile._TemporaryFileWrapper.write", create=True, side_effect=OSError(errno.ENOSPC, "secret device detail")):
                with self.assertRaises(UploadDownloadError) as caught:
                    with downloaded_upload(store=self._store(server.port), capability=self._grant(server.port), directory=Path(directory), deadline=monotonic() + 2):
                        self.fail("Failed write was accepted")
            self.assertNotIsInstance(caught.exception, UploadUnavailable)
            self.assertIn("No space left on device", str(caught.exception))
            self.assertNotIn("secret device detail", str(caught.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_store_settings_bound_actual_resource_effects(self):
        for length, timeout in ((float("nan"), 1), (True, 1), (1024, float("inf")), (1024, float("nan"))):
            with self.subTest(length=length, timeout=timeout), self.assertRaises(ValueError):
                UploadStore("https://localhost", length, timeout)
        with self.assertRaises(ValueError):
            UploadStore("https://localhost:0", 1024)
        with TemporaryDirectory() as directory, patch("secs_inference.provider.upload_download._transfer") as transfer:
            with self.assertRaises(UploadDownloadError) as caught:
                download_upload(store=replace(self._store(443), max_upload_bytes=3), capability=self._grant(443),
                                directory=Path(directory), deadline=monotonic() + 1)
            self.assertIn("4-byte Upload", str(caught.exception))
            self.assertIn("3-byte per-Upload limit", str(caught.exception))
            transfer.assert_not_called()

    def test_local_input_setup_and_buffered_flush_failures_reach_the_attempt_message(self):
        for phase in ("workspace", "creation", "finalization"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                api = FakeApi()
                def analyse(active):
                    if phase == "workspace":
                        with patch("secs_inference.provider.analysis_run.Path.mkdir", side_effect=PermissionError(errno.EACCES, "secret path")):
                            with AttemptSources(None, active, (), None, root / "current", deadline=monotonic() + 2, max_total_bytes=10):
                                self.fail("Unwritable workspace was accepted")
                    if phase == "creation":
                        arguments = {"side_effect": PermissionError(errno.EACCES, "secret path")}
                    else:
                        arguments = {"return_value": FlushFailure((root / "upload").open("wb", buffering=0))}
                    with patch("secs_inference.provider.upload_download.NamedTemporaryFile", **arguments), patch(
                            "secs_inference.provider.upload_download._transfer", side_effect=lambda _s, _c, _t, output, _d: output.write(b"data")):
                        return download_upload(store=self._store(443), capability=self._grant(443), directory=root, deadline=monotonic() + 2)
                with AttemptStore(root / "journal") as journal:
                    ExecutionLoop(api, journal, analyse, lambda *_: None).step()
                    command = json.loads(api.calls[-1])
                    self.assertEqual(command["failure_code"], "input_access_failed")
                    self.assertIn("No space left on device" if phase == "finalization" else "Permission denied", command["failure_message"])
                    self.assertNotIn("secret", command["failure_message"])
                    self.assertFalse((root / "upload").exists())

    def test_buffered_close_failure_does_not_replace_the_transfer_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = FlushFailure((root / "upload").open("wb", buffering=0))
            failure = UploadUnavailable("transfer interrupted")
            with patch("secs_inference.provider.upload_download.NamedTemporaryFile", return_value=output), patch(
                    "secs_inference.provider.upload_download._transfer", side_effect=failure), self.assertLogs(
                    "secs_inference.provider.upload_download", level="ERROR") as logged:
                with self.assertRaises(UploadUnavailable) as caught:
                    download_upload(store=self._store(443), capability=self._grant(443), directory=root, deadline=monotonic() + 2)
            self.assertIs(caught.exception, failure)
            self.assertTrue(output.closed)
            self.assertFalse((root / "upload").exists())
            self.assertIn("No space left on device", " ".join(logged.output))
            self.assertNotIn("secret", " ".join(logged.output))


class FlushFailure(io.BufferedWriter):
    """Small writes succeed; the OS failure is first observed when close flushes."""

    def flush(self):
        raise OSError(errno.ENOSPC, "secret device detail")
