"""Check pinned release asset reuse and conflict-preserving publication."""

import hashlib
import http.client
import io
import multiprocessing
import socket
import ssl
import sys
import time
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from tools import acquire_release_asset


def acquire_in_process(pool, expected, count):
    def fetch(_url):
        with count.get_lock():
            count.value += 1
        time.sleep(0.1)
        return io.BytesIO(b"verified release asset")

    arguments = ["asset", "acquire", str(pool), "tool.pyz", expected, "https://example.invalid/tool.pyz"]
    with mock.patch.object(sys, "argv", arguments), mock.patch.object(acquire_release_asset, "open_url", fetch):
        raise SystemExit(acquire_release_asset.main())


class ReleaseAssetTests(TestCase):
    def invoke(self, action, pool, expected, payload=None):
        arguments = ["asset", action, str(pool), "tool.pyz", expected, "https://example.invalid/tool.pyz"]
        source = mock.Mock(side_effect=AssertionError("unexpected download")) if payload is None else mock.Mock(return_value=io.BytesIO(payload))
        with mock.patch.object(sys, "argv", arguments), mock.patch.object(acquire_release_asset, "open_url", source):
            result = acquire_release_asset.main()
        return result, source.call_count

    def test_missing_asset_is_acquired_once_then_reused(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            payload = b"verified release asset"
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(self.invoke("check", pool, expected), (10, 0))
            self.assertEqual(self.invoke("acquire", pool, expected, payload), (0, 1))
            self.assertEqual(self.invoke("acquire", pool, expected), (0, 0))
            self.assertEqual((pool / "tool.pyz").read_bytes(), payload)
            self.assertEqual([path.name for path in pool.iterdir()], ["tool.pyz"])

    def test_conflict_and_bad_download_preserve_the_pool(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            expected = hashlib.sha256(b"expected").hexdigest()
            self.assertEqual(self.invoke("acquire", pool, expected, b"wrong")[0], 1)
            self.assertEqual(list(pool.iterdir()), [])
            (pool / "tool.pyz").write_bytes(b"other")
            self.assertEqual(self.invoke("acquire", pool, expected)[0], 1)
            self.assertEqual((pool / "tool.pyz").read_bytes(), b"other")

    def test_download_error_is_sanitized_and_removes_temporary_file(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            arguments = ["asset", "acquire", str(pool), "tool.pyz", "0" * 64, "https://example.invalid/tool.pyz"]
            with mock.patch.object(sys, "argv", arguments), redirect_stderr(io.StringIO()) as stderr, \
                    mock.patch.object(acquire_release_asset, "open_url", side_effect=ValueError("secret-token")):
                self.assertEqual(acquire_release_asset.main(), 1)
            self.assertNotIn("secret-token", stderr.getvalue())
            self.assertEqual(list(pool.iterdir()), [])

    def test_oversized_download_is_not_published(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            with mock.patch.object(acquire_release_asset, "MAX_ASSET_BYTES", 1):
                self.assertEqual(self.invoke("acquire", pool, "0" * 64, b"too large")[0], 1)
            self.assertEqual(list(pool.iterdir()), [])

    def test_slow_download_is_not_published(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            args = ["asset", "acquire", str(pool), "tool.pyz", "0" * 64, "https://example.invalid/tool.pyz"]
            with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=lambda _: io.BytesIO(b"partial")) as fetch, \
                    mock.patch.object(acquire_release_asset.time, "monotonic", side_effect=(0, 0, 301) * 3), \
                    mock.patch.object(acquire_release_asset.time, "sleep"), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(acquire_release_asset.main(), 1)
            self.assertEqual(fetch.call_count, 3)
            self.assertIn("network timeout", stderr.getvalue())
            self.assertEqual(list(pool.iterdir()), [])

    def test_interrupted_read_is_not_published(self):
        class Interrupted(io.BytesIO):
            def read1(self, size=-1):
                raise http.client.IncompleteRead(b"partial")

        with TemporaryDirectory() as directory:
            pool = Path(directory)
            arguments = ["asset", "acquire", str(pool), "tool.pyz", "0" * 64, "https://example.invalid/tool.pyz"]
            with mock.patch.object(sys, "argv", arguments), \
                    mock.patch.object(acquire_release_asset, "open_url", side_effect=lambda _: Interrupted()), \
                    mock.patch.object(acquire_release_asset.time, "sleep"), \
                    redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(acquire_release_asset.main(), 1)
            self.assertIn("download failed", stderr.getvalue())
            self.assertEqual(list(pool.iterdir()), [])

    def test_transient_read_retries_from_a_fresh_partial(self):
        class Interrupted(io.BytesIO):
            def read1(self, size=-1):
                raise ConnectionResetError(104, "private detail")

        with TemporaryDirectory() as directory:
            pool = Path(directory)
            payload = b"verified release asset"
            args = ["asset", "acquire", str(pool), "tool.pyz", hashlib.sha256(payload).hexdigest(), "https://example.invalid/tool.pyz"]
            with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=[Interrupted(), io.BytesIO(payload)]) as fetch, \
                    mock.patch.object(acquire_release_asset.time, "sleep") as sleep:
                self.assertEqual(acquire_release_asset.main(), 0)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(1)
            self.assertEqual((pool / "tool.pyz").read_bytes(), payload)
            self.assertEqual([p.name for p in pool.iterdir()], ["tool.pyz"])

    def test_permanent_and_exhausted_failures_are_safe_and_bounded(self):
        cases = [(ssl.SSLCertVerificationError("secret"), 1), (socket.gaierror(socket.EAI_AGAIN, "secret"), 3)]
        for error, calls in cases:
            with self.subTest(error=error), TemporaryDirectory() as directory:
                args = ["asset", "acquire", directory, "tool.pyz", "0" * 64, "https://example.invalid/tool.pyz"]
                with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=error) as fetch, \
                        mock.patch.object(acquire_release_asset.time, "sleep"), redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(acquire_release_asset.main(), 1)
                self.assertEqual(fetch.call_count, calls)
                self.assertNotIn("secret", stderr.getvalue())
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_retry_after_is_honored_or_rejected_without_early_retry(self):
        for delay, expected_calls in (("5", 2), ("31", 1), ("9" * 5000, 1)):
            with self.subTest(delay=delay), TemporaryDirectory() as directory:
                payload = b"verified release asset"
                args = ["asset", "acquire", directory, "tool.pyz", hashlib.sha256(payload).hexdigest(), "https://example.invalid/tool.pyz"]
                error = urllib.error.HTTPError("https://secret.invalid", 503, "private", {"Retry-After": delay}, None)
                with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=[error, io.BytesIO(payload)]) as fetch, \
                        mock.patch.object(acquire_release_asset.time, "sleep") as sleep, redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(acquire_release_asset.main(), 0 if expected_calls == 2 else 1)
                self.assertEqual(fetch.call_count, expected_calls)
                if expected_calls == 2:
                    sleep.assert_called_once_with(5)
                else:
                    sleep.assert_not_called()
                    self.assertIn("Retry-After exceeds 30 seconds", stderr.getvalue())
                self.assertNotIn("secret", stderr.getvalue())

    def test_truncated_response_and_proxy_tunnel_are_transient(self):
        class Truncated(io.BytesIO):
            length = 4

        with TemporaryDirectory() as directory:
            payload = b"verified release asset"
            args = ["asset", "acquire", directory, "tool.pyz", hashlib.sha256(payload).hexdigest(), "https://example.invalid/tool.pyz"]
            errors = [urllib.error.URLError(OSError("Tunnel connection failed: 502 private")), Truncated(b"short"), io.BytesIO(payload)]
            with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=errors) as fetch, \
                    mock.patch.object(acquire_release_asset.time, "sleep") as sleep:
                self.assertEqual(acquire_release_asset.main(), 0)
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
            self.assertEqual((Path(directory) / "tool.pyz").read_bytes(), payload)

    def test_http_408_retries(self):
        with TemporaryDirectory() as directory:
            payload = b"verified release asset"
            args = ["asset", "acquire", directory, "tool.pyz", hashlib.sha256(payload).hexdigest(), "https://example.invalid/tool.pyz"]
            error = urllib.error.HTTPError("https://example.invalid", 408, "timeout", {}, None)
            with mock.patch.object(sys, "argv", args), mock.patch.object(acquire_release_asset, "open_url", side_effect=[error, io.BytesIO(payload)]) as fetch, \
                    mock.patch.object(acquire_release_asset.time, "sleep") as sleep:
                self.assertEqual(acquire_release_asset.main(), 0)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_simultaneous_acquisition_downloads_once(self):
        with TemporaryDirectory() as directory:
            pool = Path(directory)
            expected = hashlib.sha256(b"verified release asset").hexdigest()
            count = multiprocessing.Value("i", 0)
            workers = [multiprocessing.Process(target=acquire_in_process, args=(pool, expected, count)) for _ in range(2)]
            try:
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(10)
                    self.assertFalse(worker.is_alive(), "asset acquisition did not finish")
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.pid is not None and worker.is_alive():
                        worker.terminate()
                        worker.join()
            self.assertEqual(count.value, 1)
            self.assertEqual((pool / "tool.pyz").read_bytes(), b"verified release asset")
