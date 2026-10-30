"""A late lifetime-close failure cannot replace an independent runtime failure."""

from pathlib import Path
import os
import ssl
import asyncio
import errno
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.lifetime import ProviderLifetime
from provider_core.api import ApiFailure
from provider_core.diagnostics import DiagnosticOutputRefused
from provider_core.api_config import ApiSettings, HttpsSettings
from provider_core.signing import SigningIdentity
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from secs_inference.provider import main
from secs_inference.provider.config import HelloPolicy, ProviderConfig
from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.worker_protocol import ScientificWorkerFailed, _document, encode


class MainCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_framed_scientific_failure_cannot_copy_nested_private_text_to_operator_output(self):
        marker = "PRIVATE_SYNTHETIC_SCIENTIFIC_INPUT"
        frame = {"outcome": "failed", "exception_type": marker, "frames": [
            {"file": marker, "function": marker, "line": 7}],
            "cause": {"exception_type": "OSError", "errno": errno.EIO, "reason": marker,
                      "unexpected": {"payload": marker}}, "tls_reason": marker}
        received = _document(encode(frame)[4:])
        failure = ScientificWorkerFailed(received)
        with (patch.object(main, "run_provider", new=AsyncMock(side_effect=failure)),
              patch.object(main, "write_stderr") as output):
            self.assertEqual(main.main(), 1)
        emitted = output.call_args.args[0]
        self.assertNotIn(marker.encode(), emitted)
        self.assertEqual(json.loads(emitted)["failure"]["scientific_worker"]["cause"]["errno"], errno.EIO)
        self.assertIs(failure.diagnostic, received)

    def test_late_scientific_failure_keeps_structural_child_evidence_in_fatal_output(self):
        original = RuntimeError("private scientific input")
        original.__cause__ = OSError(errno.EIO, "private scientific path")
        frame = {"outcome": "failed", **exception_evidence(original)}
        failure = BaseExceptionGroup("provider interrupted with retained observations", [
            asyncio.CancelledError("private scientific cancellation"),
            ExceptionGroup("Attempt retirement", [ScientificWorkerFailed(frame)]),
        ])
        with (patch.object(main, "run_provider", new=AsyncMock(side_effect=failure)),
              patch.object(main, "write_stderr") as output):
            self.assertEqual(main.main(), 1)
        emitted = output.call_args.args[0]
        document = json.loads(emitted)
        group = document["failure"]["relationships"][1]["failure"]
        scientific = group["relationships"][0]["failure"]["scientific_worker"]
        self.assertEqual(scientific["exception_type"], "RuntimeError")
        self.assertEqual(scientific["cause"]["errno"], errno.EIO)
        self.assertIs(failure.exceptions[1].exceptions[0].diagnostic, frame)
        self.assertNotIn(b"private scientific", emitted)

    def test_contained_background_exit_is_nonzero_without_a_private_traceback(self):
        for code in (0, None, "private fixture detail"):
            with self.subTest(code=code):
                script = (
                    "from secs_inference.provider import main\n"
                    "async def fail():\n"
                    f"    raise BaseExceptionGroup('contained exit', [SystemExit({code!r})])\n"
                    "main.run_provider = fail\n"
                    "raise SystemExit(main.main())\n"
                )
                result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b'"exception_type": "SystemExit"', result.stderr)
                self.assertNotIn(b"private fixture detail", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_reporting_failure_retains_original_uncertainty_and_error_objects(self):
        primary = ApiFailure("complete Execution Attempt", "the reply was not confirmed")
        primary.request_method = "POST"
        primary.request_target = b"/provider/v1/execution-attempts/complete"
        primary.attempt_mutation_may_have_committed = True
        for reporting in (DiagnosticOutputRefused("fixture output refusal"), KeyboardInterrupt(),
                          SystemExit(0), asyncio.CancelledError()):
            with self.subTest(reporting=type(reporting).__name__):
                with (patch.object(main, "run_provider", new=AsyncMock(side_effect=primary)),
                      patch.object(main, "write_stderr", side_effect=reporting)):
                    try:
                        main.main()
                    except BaseException as caught:
                        observed = caught
                    else:
                        self.fail("failed diagnostic output returned without its failure exit")
                self.assertIs(type(observed), SystemExit)
                self.assertEqual(observed.code, 1)
                self.assertIsInstance(observed.__cause__, BaseExceptionGroup)
                self.assertEqual(len(observed.__cause__.exceptions), 2)
                self.assertIs(observed.__cause__.exceptions[0], primary)
                self.assertIs(observed.__cause__.exceptions[1], reporting)
                self.assertTrue(primary.attempt_mutation_may_have_committed)
                self.assertEqual(primary.request_target, b"/provider/v1/execution-attempts/complete")

    def test_partial_fatal_write_does_not_add_a_raw_private_traceback(self):
        script = """from secs_inference.provider import main
from provider_core import diagnostics
async def fail():
    raise RuntimeError("private-primary-payload")
original_write = diagnostics.os.write
def partial(descriptor, content):
    return original_write(descriptor, content[:37])
main.run_provider = fail
diagnostics.os.write = partial
raise SystemExit(main.main())
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(b"private-primary-payload" in result.stderr, "primary payload escaped after output failure")
        self.assertFalse(b"Traceback" in result.stderr, "CLI emitted an uncontrolled traceback")
        self.assertEqual(len(result.stderr), 37)

    async def test_lifetime_close_and_runtime_failures_both_reach_process_owner(self):
        runtime_failure = BaseExceptionGroup("contained exit", [SystemExit("private fixture detail")])
        close_failure = OSError("fixture lifetime close failure")
        hello, discovery = SimpleNamespace(retire=AsyncMock()), SimpleNamespace(retire=AsyncMock())
        settings = ApiSettings(SigningIdentity("https://api.test", "credential:fixture", Ed25519PrivateKey.generate()),
                               HttpsSettings(ssl.create_default_context(), 1, 1), "dev-local")
        close = ProviderLifetime.close
        def failing_close(lifetime):
            close(lifetime)
            raise close_failure
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ProviderConfig(root / "settings", {}, 1, 1, 5, HelloPolicy("SECS", "fixture", 10, 1))
            grants = SimpleNamespace(proxy_socket=Path("/route/proxy.sock"))
            with (
                patch.object(main, "LIFETIME_DIRECTORY", root),
                patch.dict(os.environ, {"SECS_API_PROXY_SOCKET": "/route/proxy.sock"}),
                patch.object(main, "read_startup_file", return_value=b"fixture"),
                patch.object(main, "decode_provider_config", return_value=config),
                patch.object(main, "shared_grants", return_value=grants),
                patch.object(main, "api_settings_with_material", return_value=(settings, "provider:fixture", object())),
                patch.object(main, "ProviderAPI", side_effect=(hello, discovery)),
                patch.object(main, "publish_hello_until_stopped", side_effect=runtime_failure),
                patch.object(ProviderLifetime, "close", autospec=True, side_effect=failing_close),
            ):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await main.run_provider(root / "provider.toml")
            self.assertEqual(caught.exception.exceptions, (runtime_failure, close_failure))
            hello.retire.assert_awaited_once()
            discovery.retire.assert_awaited_once()
