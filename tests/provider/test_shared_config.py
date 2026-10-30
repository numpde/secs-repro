"""SECS grants exactly the API files selected by its provider configuration."""

from pathlib import Path
import unittest

from secs_inference.provider.config import decode_provider_config
from secs_inference.provider.configuration_error import ConfigurationError


class SharedConfigTests(unittest.TestCase):
    def test_example_uses_installed_credential_and_local_shared_grant(self):
        raw = (Path(__file__).resolve().parents[2] / "config/provider.toml.example").read_bytes()
        config = decode_provider_config(raw)
        self.assertEqual(config.api["credential_file"], "/run/secrets/provider/provider.signing.private.json")
        self.assertEqual(config.shared_project, "replace-me")
        self.assertEqual(config.shared_gid, 1)
        self.assertEqual(config.delivery_retry_max_seconds, 30)

    def test_shared_project_and_gid_are_validated(self):
        raw = (Path(__file__).resolve().parents[2] / "config/provider.toml.example").read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "Shared project"):
            decode_provider_config(raw.replace(b'project = "replace-me"', b'project = "../shared"'))
        with self.assertRaisesRegex(ConfigurationError, "Shared gid"):
            decode_provider_config(raw.replace(b"gid = 1", b"gid = 0"))

    def test_ungranted_credential_and_ca_paths_are_rejected(self):
        raw = (Path(__file__).resolve().parents[2] / "config/provider.toml.example").read_bytes()
        credential = b'/run/secrets/provider/provider.signing.private.json'
        with self.assertRaisesRegex(ConfigurationError, "installed provider credential"):
            decode_provider_config(raw.replace(credential, b'/private/elsewhere.json'))
        with self.assertRaisesRegex(ConfigurationError, "ca_file"):
            decode_provider_config(raw.replace(b'# ca_file = "/run/config/provider/api-ca.crt"',
                                               b'ca_file = "/private/elsewhere.crt"'))
