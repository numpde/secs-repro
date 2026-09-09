"""Test SECS-specific installation and attempt ownership without a model or engine."""

import json
import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.compose import ComposeProject
from deployment.provider_deployment import _bind_attempt_owner, install_secret, main
from deployment.templates import _locked_parent


class ProviderDeploymentTests(unittest.TestCase):
    def test_installation_reports_publication_when_staging_cleanup_fails(self):
        for sync_fails in (False, True):
            with self.subTest(sync_fails=sync_fails), TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config/deployments/production"
                config.mkdir(parents=True, mode=0o700)
                source = root / "key"
                source.write_bytes(b"test-only secret")
                source.chmod(0o600)
                destination = root / "secrets/deployments/production/interpreter.key"
                output = StringIO()

                def sync_after_link(path):
                    """Inject a sync failure only after the complete key becomes visible."""
                    if sync_fails and destination.exists():
                        raise OSError("publication sync failed")

                with patch("deployment.provider_deployment.__file__", str(root / "deployment/cli.py")), patch(
                    "deployment.templates._sync_directory", side_effect=sync_after_link,
                ), patch.object(TemporaryDirectory, "_rmtree", side_effect=OSError("staging cleanup denied")), \
                        redirect_stderr(output):
                    self.assertEqual(main(["interpreter-key-install", "production", "--source", str(source)]), 1)
                self.assertEqual(destination.read_bytes(), b"test-only secret")
                self.assertIn(f"File is visible at {destination}", output.getvalue())
                self.assertIn("staging cleanup denied", output.getvalue())
                if sync_fails:
                    self.assertIn("publication sync failed", output.getvalue())
                    self.assertIn("durability is unconfirmed", output.getvalue())

    def test_lifecycle_mutations_still_hold_the_exclusive_lock(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "production"
            config.mkdir(mode=0o700)

            def check_exclusion(*args):
                """A separate open cannot acquire the lock while a mutation owns it."""
                descriptor = os.open(config.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                return {}

            with patch("deployment.provider_deployment.configuration_directory", return_value=config), patch(
                "deployment.provider_deployment.start_deployment", side_effect=check_exclusion,
            ), patch.object(ComposeProject, "stop", side_effect=check_exclusion), redirect_stdout(StringIO()):
                for operation in ("up", "down"):
                    with self.subTest(operation=operation):
                        self.assertEqual(main([operation, "production"]), 0)

    def test_observation_does_not_wait_for_a_lifecycle_lock(self):
        with TemporaryDirectory() as temporary:
            config = Path(temporary) / "production"
            config.mkdir(mode=0o700)
            with patch("deployment.provider_deployment.configuration_directory", return_value=config), patch(
                "deployment.provider_deployment.render_deployment", return_value={},
            ), patch.object(ComposeProject, "inventory", return_value={}), redirect_stdout(StringIO()), \
                    ThreadPoolExecutor(max_workers=1) as pool:
                for operation in ("status", "logs", "config"):
                    with self.subTest(operation=operation), _locked_parent(config.parent):
                        # A shutdown may hold this inode for its entire grace
                        # period. Observations must finish before it releases it.
                        observed = pool.submit(main, [operation, "production"])
                        self.assertEqual(observed.result(timeout=5), 0)

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
