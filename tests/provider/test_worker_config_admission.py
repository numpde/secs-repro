"""The scientific child admits its actual configuration before loading a model."""

import contextlib
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from secs_inference.provider import worker_model


class WorkerConfigAdmissionTests(unittest.TestCase):
    def test_missing_configuration_reports_safe_original_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-worker-path.toml"
            error = io.StringIO()
            with patch.object(worker_model, "Path", return_value=path), contextlib.redirect_stderr(error):
                status = worker_model.main(["--session-fd", "-1", "--lease-fd", "-1", "--operation", "test"])
            evidence = json.loads(error.getvalue().splitlines()[1])
            self.assertEqual(status, 1)
            self.assertEqual(evidence["cause"]["read_cause"]["errno"], errno.ENOENT)
            self.assertNotIn("context", evidence["cause"])
            self.assertNotIn("private-worker-path.toml", json.dumps(evidence))

    def test_valid_configuration_reaches_session_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.toml"
            path.write_text('checkpoint_directory = "/checkpoint"\nmolformer_lock = "/input/lock.toml"\n')
            path.chmod(0o600)
            error = io.StringIO()
            with patch.object(worker_model, "Path", return_value=path), contextlib.redirect_stderr(error):
                status = worker_model.main(["--session-fd", "-1", "--lease-fd", "-1", "--operation", "test"])
            self.assertEqual(status, 1)
            self.assertIn("adopting the inherited scientific socket", error.getvalue())
            self.assertIn("require distinct open descriptors", error.getvalue())

    def test_oversized_configuration_is_rejected_at_child_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.toml"
            path.write_bytes(b" " * 65537)
            error = io.StringIO()
            with patch.object(worker_model, "Path", return_value=path), contextlib.redirect_stderr(error):
                status = worker_model.main(["--session-fd", "-1", "--lease-fd", "-1", "--operation", "test"])
            self.assertEqual(status, 1)
            self.assertIn("within 65536 bytes", error.getvalue())
            self.assertIn("reading scientific worker configuration", error.getvalue())


if __name__ == "__main__":
    unittest.main()
