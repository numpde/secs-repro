"""A late lifetime-close failure cannot replace an independent runtime failure."""

from pathlib import Path
import os
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from provider_core.lifetime import ProviderLifetime
from provider_core.api_config import ApiSettings, HttpsSettings
from provider_core.signing import SigningIdentity
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from secs_inference.provider import main
from secs_inference.provider.config import HelloPolicy, ProviderConfig


class MainCleanupTests(unittest.IsolatedAsyncioTestCase):
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
            config = ProviderConfig(root / "definition", "secs", HelloPolicy("SECS", "fixture", 10, 1))
            definition = SimpleNamespace(selected=lambda _name: SimpleNamespace(api={}))
            with (
                patch.object(main, "LIFETIME_DIRECTORY", root),
                patch.dict(os.environ, {"SECS_API_PROXY_SOCKET": "/route/proxy.sock"}),
                patch.object(main, "read_startup_file", return_value=b"fixture"),
                patch.object(main, "decode_provider_config", return_value=config),
                patch.object(main, "load_deployment_bindings", return_value=definition),
                patch.object(main, "api_settings", return_value=(settings, "provider:fixture")),
                patch.object(main, "ProviderAPI", side_effect=(hello, discovery)),
                patch.object(main, "publish_hello_until_stopped", side_effect=runtime_failure),
                patch.object(ProviderLifetime, "close", autospec=True, side_effect=failing_close),
            ):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await main.run_provider(root / "provider.toml")
            self.assertEqual(caught.exception.exceptions, (runtime_failure, close_failure))
            hello.retire.assert_awaited_once()
            discovery.retire.assert_awaited_once()
