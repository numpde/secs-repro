"""SECS grants exactly the API files selected by its provider configuration."""

from pathlib import Path
import unittest

from secs_inference.provider.config import decode_provider_config
from secs_inference.provider.configuration_error import ConfigurationError


class SharedConfigTests(unittest.TestCase):
    def test_example_uses_installed_credential_and_valid_shared_settings(self):
        raw = (Path(__file__).resolve().parents[2] / "config/provider.toml.example").read_bytes()
        config = decode_provider_config(raw)
        self.assertEqual(config.api["credential_file"], "/run/secrets/provider/provider.signing.private.json")
        self.assertEqual(config.delivery_retry_max_seconds, 30)

    def test_ungranted_credential_and_ca_paths_are_rejected(self):
        raw = (Path(__file__).resolve().parents[2] / "config/provider.toml.example").read_bytes()
        credential = b'/run/secrets/provider/provider.signing.private.json'
        with self.assertRaisesRegex(ConfigurationError, "installed provider credential"):
            decode_provider_config(raw.replace(credential, b'/private/elsewhere.json'))
        with self.assertRaisesRegex(ConfigurationError, "ca_file"):
            decode_provider_config(raw.replace(b'# ca_file = "/run/config/provider/api-ca.crt"',
                                               b'ca_file = "/private/elsewhere.crt"'))
