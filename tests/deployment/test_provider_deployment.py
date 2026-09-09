"""Test SECS-specific installation and attempt ownership without a model or engine."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.provider_deployment import _bind_attempt_owner, install_secret


class ProviderDeploymentTests(unittest.TestCase):
    def test_failed_owner_write_does_not_poison_the_next_start(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider, credential = root / "provider.toml", root / "provider.signing.private.json"
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            credential.write_text('{"principal_ref": "provider:test"}')
            for path in (provider, credential):
                path.chmod(0o600)

            def interrupted_write(output, content):
                """Simulate a disk write failure after the first byte reaches the file."""
                output.write_bytes(content[:1])
                raise OSError("disk full")

            with patch("deployment.templates._write_new_file", side_effect=interrupted_write):
                with self.assertRaisesRegex(OSError, "disk full"):
                    _bind_attempt_owner(root, root)
            self.assertFalse((root / "attempt-owner.json").exists())
            _bind_attempt_owner(root, root)
            self.assertEqual(json.loads((root / "attempt-owner.json").read_bytes()),
                             {"origin": "https://api.example.test", "provider_ref": "provider:test"})

    def test_credential_install_is_private_and_never_replaces_existing_input(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config/deployments/production"
            config.mkdir(parents=True, mode=0o700)
            source = root / "source"
            source.write_text("test-only credential bytes")
            source.chmod(0o600)
            installed = install_secret(root, "production", "provider.signing.private.json", source)
            self.assertEqual(installed.read_bytes(), source.read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                install_secret(root, "production", "provider.signing.private.json", source)
            self.assertTrue(source.exists())

    def test_attempt_state_allows_key_rotation_but_not_another_api_or_provider(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, config = root / "state", root / "config"
            state.mkdir(mode=0o700)
            config.mkdir(mode=0o700)
            provider = config / "provider.toml"
            credential = state / "provider.signing.private.json"
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            provider.chmod(0o600)
            credential.write_text(json.dumps({"principal_ref": "provider:test", "credential_ref": "first"}))
            credential.chmod(0o600)
            _bind_attempt_owner(state, config)
            credential.write_text(json.dumps({"principal_ref": "provider:test", "credential_ref": "rotated"}))
            _bind_attempt_owner(state, config)
            binding = (state / "attempt-owner.json").read_bytes()
            provider.write_text('[api]\norigin="https://another.example.test"\n')
            with self.assertRaisesRegex(ValueError, "another API or provider"):
                _bind_attempt_owner(state, config)
            provider.write_text('[api]\norigin="https://api.example.test"\n')
            credential.write_text(json.dumps({"principal_ref": "provider:other"}))
            with self.assertRaisesRegex(ValueError, "another API or provider"):
                _bind_attempt_owner(state, config)
            self.assertEqual((state / "attempt-owner.json").read_bytes(), binding)


if __name__ == "__main__":
    unittest.main()
