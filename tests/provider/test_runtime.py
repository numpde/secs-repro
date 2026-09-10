"""Production composition preserves ownership through shutdown and recovery."""

from pathlib import Path
from io import StringIO
import errno
import json
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.config import ExecutionConfig, HelloPolicy, ProviderConfig, decode_provider_config
from secs_inference.provider.credential import parse_provider_credential
from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.execution import ProviderStopping
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.job_api import ApiError, ApiUnavailable
from secs_inference.provider.chat import ChatEndpoint
from secs_inference.provider.http import HttpsEndpoint
from secs_inference.provider.upload_download import UploadStore
from secs_inference.provider.main import main, run_provider, _read_regular_file
from secs_inference.provider.runtime import run_execution, run_services
from secs_inference.provider.configuration_error import ConfigurationError
from test_execution import START


CONFIG = ExecutionConfig("https://model.test/chat", "model", "https://store.test")


class RuntimeTests(unittest.TestCase):
    def test_concurrent_hello_failure_is_reported_without_replacing_execution_failure(self):
        stop = Event()
        hello_failed = Event()
        config = ProviderConfig(None, HelloPolicy("Test", "Description", 60, 1), CONFIG)
        execution_failure = RuntimeError("private-execution")
        original_cause = OSError(errno.EIO, "private-original")
        execution_failure.__cause__ = original_cause
        hello_failure = ApiError("Provider hello returned HTTP 400 for request hello-request")
        hello_failure.__cause__ = ValueError("private-hello")
        def fail_hello(**kwargs):
            hello_failed.set()
            raise hello_failure
        def fail_execution(**kwargs):
            self.assertTrue(hello_failed.wait(2))
            raise execution_failure
        with TemporaryDirectory() as directory, patch("secs_inference.provider.runtime.JOURNAL_DIRECTORY", Path(directory) / "journal"), \
             patch("secs_inference.provider.runtime.publish_hello_until_stopped", side_effect=fail_hello), \
             patch("secs_inference.provider.runtime.run_execution", side_effect=fail_execution), \
             self.assertLogs("secs_inference.provider.runtime", level="ERROR") as captured:
            with self.assertRaises(RuntimeError) as caught:
                run_services(api=Mock(), prepared=None, config=config, chat=None, upload_store=None, stop=stop)
            with AttemptStore(Path(directory) / "journal"):
                pass
        self.assertIs(caught.exception, execution_failure)
        self.assertIs(execution_failure.__cause__, original_cause)
        text = "\n".join(captured.output)
        self.assertIn("hello also failed", text)
        self.assertIn("hello-request", text)
        self.assertIn("HTTP 400", text)
        self.assertNotIn("private-", text)

    def test_malformed_endpoint_configuration_is_actionable_without_echoing_values(self):
        for endpoint in ("interpreter", "upload store"):
            for url in (42, "https://[private-invalid", "https://model.test:private-port", "https://model.test:70000", "https://model.test:0"):
                with self.subTest(endpoint=endpoint, url=url):
                    def configure():
                        return ChatEndpoint(url, "model", "key") if endpoint == "interpreter" else UploadStore(url, 100)
                    output = StringIO()
                    with patch("secs_inference.provider.main.run_provider", side_effect=configure), patch("sys.stderr", output):
                        self.assertEqual(main(), 1)
                    message = output.getvalue()
                    self.assertIn(endpoint, message.lower())
                    self.assertIn("HTTPS", message)
                    self.assertIn("1-65535", message)
                    self.assertNotIn("unexpected internal error", message)
                    self.assertNotIn("private-", message)

    def test_terminal_output_keeps_causal_evidence_without_exception_text_or_notes(self):
        def failed_runtime():
            try:
                raise OSError(errno.ENOSPC, "private-source-data", "/private-input")
            except OSError:
                error = RuntimeError("private-wrapper-data")
                error.add_note("private-note-data")
                raise error from None
        output = StringIO()
        with patch("secs_inference.provider.main.run_provider", side_effect=failed_runtime), patch("sys.stderr", output):
            self.assertEqual(main(), 1)
        message = output.getvalue()
        self.assertIn("unexpected internal error", message)
        evidence = json.loads(message.splitlines()[1])
        self.assertEqual(evidence["context"]["errno"], errno.ENOSPC)
        self.assertEqual(evidence["context"]["exception_type"], "OSError")
        self.assertNotIn("private-", message)

    def test_owned_configuration_explanation_is_not_lost_with_hidden_notes(self):
        error = ConfigurationError("Cannot configure the provider: interpreter_model is missing")
        error.add_note("private-note-data")
        output = StringIO()
        with patch("secs_inference.provider.main.run_provider", side_effect=error), patch("sys.stderr", output):
            self.assertEqual(main(), 1)
        self.assertIn("interpreter_model is missing", output.getvalue())
        self.assertNotIn("private-note", output.getvalue())

    def test_credential_errors_explain_expected_input_without_echoing_key_material(self):
        document = {
            "algorithm": "ed25519", "credential_ref": "credential:test",
            "principal_ref": "provider:test", "profile": "run",
            "schema_id": "nmr.provider.private_signing_credential.v1",
            "public_key_spki_der_b64": "private-key-content",
            "private_key_pkcs8_pem": "private-key-content",
        }
        for bad, expected in (
            (document | {"algorithm": "private-key-content"}, "algorithm='ed25519'"),
            (document, "expected a Base64 DER public key and an unencrypted PEM private key"),
            (document | {"private-key-content": "secret"}, "exactly these API-issued fields"),
        ):
            with self.subTest(expected=expected):
                raw = canonical_json_bytes(bad) + b"\n"
                output = StringIO()
                with patch("secs_inference.provider.main.run_provider", side_effect=lambda: parse_provider_credential(raw)), patch("sys.stderr", output):
                    self.assertEqual(main(), 1)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("private-key-content", output.getvalue())

    def test_config_errors_name_missing_fields_without_echoing_unknown_keys_or_values(self):
        raw = Path("/workspace/config/provider.toml.example").read_bytes()
        broken = raw.replace(b"interpreter_model =", b"private_pasted_key =")
        output = StringIO()
        with patch("secs_inference.provider.main.run_provider", side_effect=lambda: decode_provider_config(broken)), patch("sys.stderr", output):
            self.assertEqual(main(), 1)
        message = output.getvalue()
        self.assertIn("missing required fields: interpreter_model", message)
        self.assertIn("1 unrecognized field", message)
        self.assertIn("allowed fields:", message)
        self.assertNotIn("private_pasted_key", message)

    def test_feed_outage_reports_its_reason_without_claiming_an_admitted_attempt(self):
        stop = Event()
        api = Mock(provider_ref="provider:test")
        def unavailable(*args, **kwargs):
            stop.set()
            raise ApiUnavailable("Provider API request to list available Jobs returned HTTP 503 for request request-test",
                                 diagnostic={"operation": "list available Jobs", "delivery": "response_received", "status": 503})
        api.request.side_effect = unavailable
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            with self.assertLogs("secs_inference.provider.runtime", level="WARNING") as logged:
                run_execution(api=api, config=CONFIG, chat=None, upload_store=None, stop=stop, journal=journal)
            self.assertIsNone(journal.load())
            message = " ".join(logged.output)
            self.assertIn("any pending Attempt state", message)
            self.assertIn("list available Jobs returned HTTP 503 for request request-test", message)
            self.assertIn(f"in {CONFIG.poll_seconds:g} seconds", message)
            self.assertIn('"delivery": "response_received"', message)

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
                    self.assertIn("could not load the CA certificates" if malformed else "No such file or directory", output.getvalue())
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
                        self.before_start(START)
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
        self.assertEqual(chat.call_args.kwargs["reasoning_effort"], "none")
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
        safe = ApiError("The Provider API rejected hello: HTTP 400 for request request-test")
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
