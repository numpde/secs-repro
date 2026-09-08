"""Production composition preserves ownership through shutdown and recovery."""

from pathlib import Path
from io import StringIO
import errno
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig, decode_provider_config
from secs_inference.provider.execution import ProviderStopping
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.job_api import ApiUnavailable
from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.http import HttpsEndpoint
from secs_inference.provider.upload_download import UploadStore
from secs_inference.provider.main import main, run_provider, _read_regular_file
from secs_inference.provider.runtime import run_execution, run_services


CONFIG = ExecutionConfig("https://model.test/chat", "model", "https://store.test")


class RuntimeTests(unittest.TestCase):
    def test_feed_outage_reports_its_reason_without_claiming_an_admitted_attempt(self):
        stop = Event()
        api = Mock(provider_ref="provider:test")
        def unavailable(*args, **kwargs):
            stop.set()
            raise ApiUnavailable("Provider API request to list available Jobs returned HTTP 503 for request request-test")
        api.request.side_effect = unavailable
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            with self.assertLogs("secs_inference.provider.runtime", level="WARNING") as logged:
                run_execution(api=api, config=CONFIG, chat=None, upload_store=None, stop=stop, journal=journal)
            self.assertIsNone(journal.load())
            message = " ".join(logged.output)
            self.assertIn("any pending Attempt state", message)
            self.assertIn("list available Jobs returned HTTP 503 for request request-test", message)

    def test_startup_stderr_distinguishes_missing_and_unreadable_inputs(self):
        for kind, reason in (("missing", "No such file or directory"), ("denied", "Permission denied"),
                             ("directory", "not a regular file"), ("oversize", "1024-byte limit")):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                path = Path(directory) / "signing.private.json"
                if kind == "directory":
                    path.mkdir()
                elif kind != "missing":
                    path.write_text("credential-content-must-not-appear" * 100)
                if kind == "denied":
                    path.chmod(0)
                output = StringIO()
                with patch("secs_inference.provider.main.run_provider", side_effect=lambda: _read_regular_file(path, 1024)), patch("sys.stderr", output):
                    self.assertEqual(main(), 1)
                self.assertIn(str(path), output.getvalue())
                self.assertIn(reason, output.getvalue())
                self.assertNotIn("credential-content-must-not-appear", output.getvalue())

    def test_opened_startup_file_failures_keep_path_and_safe_reason(self):
        for outcome, reason in ((OSError(errno.EIO, "secret error detail"), "Input/output error"), (b"", "changed while it was read")):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                path = Path(directory) / "provider.toml"
                path.write_bytes(b"private startup bytes")
                output = StringIO()
                fault = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("secs_inference.provider.main.os.read", **fault), patch(
                        "secs_inference.provider.main.run_provider", side_effect=lambda: _read_regular_file(path, 1024)), patch("sys.stderr", output):
                    self.assertEqual(main(), 1)
                self.assertIn(str(path), output.getvalue())
                self.assertIn(reason, output.getvalue())
                self.assertNotIn("secret error detail", output.getvalue())
                self.assertNotIn("private startup bytes", output.getvalue())

    def test_each_tls_startup_failure_names_its_own_trust_input(self):
        owners = (
            ("Provider API", lambda path: HttpsEndpoint("https://api.test", "web", 1, 1, path)),
            ("interpreter", lambda path: ChatEndpoint("https://model.test/chat", "model", "key", path)),
            ("Upload store", lambda path: UploadStore("https://store.test", 1024, ca_file=path)),
        )
        for role, configure in owners:
            for malformed in (False, True):
                with self.subTest(role=role, malformed=malformed), TemporaryDirectory() as directory:
                    path = Path(directory) / "ca.pem"
                    if malformed:
                        path.write_text("private-invalid-certificate-content")
                    output = StringIO()
                    with patch("secs_inference.provider.main.run_provider", side_effect=lambda: configure(path)), patch("sys.stderr", output):
                        self.assertEqual(main(), 1)
                    self.assertIn(f"Cannot load {role} TLS trust from {path}", output.getvalue())
                    self.assertIn("SSLError" if malformed else "No such file or directory", output.getvalue())
                    self.assertNotIn("private-invalid-certificate-content", output.getvalue())

    def test_reused_worker_cleanup_precedes_next_remote_admission(self):
        for cleanup_fails in (True, False):
            with self.subTest(cleanup_fails=cleanup_fails), TemporaryDirectory() as directory:
                root = Path(directory) / "current"
                stop = Event()
                worker = Mock(stopped=False)
                admissions = []
                class Loop:
                    def __init__(self, jobs, journal, analyse, diagnose, before_start):
                        self.before_start = before_start
                    def step(self):
                        self.before_start()
                        admissions.append(True)
                        if len(admissions) == 1:
                            root.mkdir()
                            return True
                        stop.set()
                        return True
                with patch("secs_inference.provider.runtime.ExecutionLoop", Loop), patch(
                        "secs_inference.provider.runtime.WorkerClient", return_value=worker) as connect, patch(
                        "secs_inference.provider.runtime.SOURCE_DIRECTORY", root):
                    if cleanup_fails:
                        with patch("secs_inference.provider.runtime.shutil.rmtree", side_effect=PermissionError("read only")):
                            with self.assertRaises(PermissionError):
                                run_execution(api=Mock(), config=CONFIG, chat=None, upload_store=None, stop=stop, journal=Mock())
                        self.assertEqual(len(admissions), 1)
                    else:
                        run_execution(api=Mock(), config=CONFIG, chat=None, upload_store=None, stop=stop, journal=Mock())
                        self.assertEqual(len(admissions), 2)
                        self.assertFalse(root.exists())
                    connect.assert_called_once()
                    worker.stop.assert_called_once()

    def test_production_wires_each_private_ca_to_its_own_endpoint(self):
        example = Path("/workspace/config/provider.toml.example").read_bytes()
        example += b"\ninterpreter_use_private_ca = true\nupload_store_use_private_ca = true\n"
        with patch("secs_inference.provider.main._read_regular_file", side_effect=(example, b"credential", b"key\n")), patch(
                "secs_inference.provider.main.parse_provider_credential"), patch("secs_inference.provider.main.ProviderApi"), patch(
                "secs_inference.provider.main.ChatEndpoint") as chat, patch("secs_inference.provider.main.UploadStore") as store, patch(
                "secs_inference.provider.main.run_services") as services:
            run_provider()
        self.assertEqual(chat.call_args.args[-1], Path("/run/config/provider/interpreter-ca.crt"))
        self.assertEqual(store.call_args.kwargs["ca_file"], Path("/run/config/provider/upload-store-ca.crt"))
        services.assert_called_once()

    def test_requested_stop_during_readiness_is_normal_and_keeps_pending_start(self):
        stop = Event()
        journal = Mock()
        with patch("secs_inference.provider.runtime.ExecutionLoop") as loop:
            loop.return_value.step.side_effect = ProviderStopping("requested")
            run_execution(api=Mock(), config=CONFIG, chat=None, upload_store=None, stop=stop, journal=journal)
        journal.clear.assert_not_called()

    def test_hello_failure_stops_execution_and_is_relayed_after_journal_release(self):
        stop = Event()
        config = ProviderConfig(None, HelloPolicy("Test", "Description", 60, 1), CONFIG)
        safe = RuntimeError("The Provider API rejected hello: HTTP 400 for request request-test")
        safe.__cause__ = ValueError("secret marker must stay hidden")
        output = StringIO()
        with TemporaryDirectory() as directory, patch("secs_inference.provider.runtime.JOURNAL_DIRECTORY", Path(directory) / "journal"):
            def run():
                run_services(api=Mock(), prepared=None, config=config, chat=None, upload_store=None, stop=stop)
            with patch("secs_inference.provider.runtime.publish_hello_until_stopped", side_effect=safe), patch(
                    "secs_inference.provider.runtime.run_execution", side_effect=lambda **kw: self.assertTrue(kw["stop"].wait(2))), patch(
                    "secs_inference.provider.main.run_provider", side_effect=run), patch("sys.stderr", output):
                self.assertEqual(main(), 1)
        self.assertIn(str(safe), output.getvalue())
        self.assertNotIn("secret marker", output.getvalue())
        self.assertIn("If Attempt state or diagnostics were retained", output.getvalue())

    def test_execution_is_explicit_and_private_ca_choices_are_independent(self):
        example = Path("/workspace/config/provider.toml.example").read_bytes()
        self.assertIsNotNone(decode_provider_config(example).execution)
        self.assertIsNone(decode_provider_config(example.split(b"[execution]")[0]).execution)
        config = decode_provider_config(example + b"\ninterpreter_use_private_ca = true\nupload_store_use_private_ca = false\n")
        self.assertTrue(config.execution.interpreter_use_private_ca)
        self.assertFalse(config.execution.upload_store_use_private_ca)
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            decode_provider_config(example + b"\nupload_store_use_private_ca = 1\n")
