"""The runner's children use one captured worker configuration per container."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from secs_inference.provider.worker_config_snapshot import capture
from provider_core.startup_file import StartupFileRejected


class WorkerConfigSnapshotTests(unittest.TestCase):
    def test_failed_snapshot_write_removes_only_its_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source.toml", root / "snapshot.toml"
            source.write_text("seed = 42\n")
            source.chmod(0o600)
            with patch("secs_inference.provider.worker_config_snapshot.os.fchmod", side_effect=OSError("refused")):
                with self.assertRaises(OSError):
                    capture(source, destination, "cuda:0")
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_text(), "seed = 42\n")

    def test_invalid_source_does_not_create_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source.toml", root / "snapshot.toml"
            source.write_bytes(b" " * 65537)
            source.chmod(0o600)
            with self.assertRaises(StartupFileRejected):
                capture(source, destination, "cuda:0")
            self.assertFalse(destination.exists())

    def test_snapshot_keeps_original_bytes_when_operator_edits_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source.toml", root / "snapshot.toml"
            source.write_bytes(b"seed = 42\n")
            source.chmod(0o600)
            previous = os.umask(0o777)
            try:
                capture(source, destination, "cuda:0")
            finally:
                os.umask(previous)
            source.write_bytes(b"seed = 99\n")
            self.assertEqual(destination.read_bytes(), b"seed = 42\n")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o400)
            with self.assertRaises(FileExistsError):
                capture(source, destination, "cuda:0")
            self.assertEqual(destination.read_bytes(), b"seed = 42\n")

    def test_changed_device_is_rejected_before_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source", Path(directory) / "snapshot"
            source.write_text('device = "cuda:0"\n')
            source.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "differs from the deployed"):
                capture(source, destination, "cpu")
            self.assertFalse(destination.exists())

    def test_changed_index_mode_is_rejected_before_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source", Path(directory) / "snapshot"
            source.write_text('device = "cpu"\nmock_index = true\n')
            source.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "differs from the deployed"):
                capture(source, destination, "cpu", False)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
