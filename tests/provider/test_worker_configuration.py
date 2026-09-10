"""Worker startup explains its own failures without loading models or credentials."""

import errno
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from secs_inference.provider.worker import WorkerStopUnconfirmed
from secs_inference.provider.worker_model import ScientificWorkerConfig, main


CONFIG = b'checkpoint_directory = "/checkpoint"\nmolformer_lock = "/input/molformer.lock.toml"\n'


class WorkerConfigurationTests(unittest.TestCase):
    def test_rejected_configuration_explains_phase_without_echoing_input(self):
        cases = (
            (OSError(errno.ENOENT, "private-file"), "reading", "No such file or directory"),
            (OSError(errno.EACCES, "private-file"), "reading", "Permission denied"),
            (b"private-invalid-toml", "parsing", "valid TOML syntax"),
            (b"\xffprivate", "parsing", "UTF-8"),
            (b"", "configuring", "requires checkpoint_directory, molformer_lock"),
            (CONFIG + b'"private-key" = "private-value"\n', "configuring", "supported fields"),
            (CONFIG + b'compute_dtype = "private-value"\n', "configuring", "float32 or bfloat16"),
            (CONFIG + b'compute_dtype = []\n', "configuring", "float32 or bfloat16"),
            (CONFIG + b'threads = true\n', "configuring", "threads must be a positive integer"),
            (CONFIG + b'seed = -1\n', "configuring", "seed must be a nonnegative integer"),
            (CONFIG + b'device = []\n', "configuring", "device must be a nonempty string"),
        )
        for raw, phase, reason in cases:
            with self.subTest(phase=phase, reason=reason):
                fault = {"side_effect": raw} if isinstance(raw, Exception) else {"return_value": raw}
                output = StringIO()
                with patch.object(Path, "read_bytes", **fault), patch("sys.stderr", output), \
                     patch("secs_inference.provider.worker.serve_worker") as serve:
                    self.assertEqual(main(), 1)
                serve.assert_not_called()
                text = output.getvalue()
                self.assertIn(f"while {phase}", text)
                self.assertIn("/run/config/worker/worker.toml", text)
                self.assertIn(reason, text)
                self.assertNotIn("private-", text)
                self.assertIn("exception_type", json.loads(text.splitlines()[1]))

    def test_valid_configuration_reaches_supervisor_without_loading_models(self):
        with patch.object(Path, "read_bytes", return_value=CONFIG), \
             patch("secs_inference.provider.worker.serve_worker") as serve, \
             patch.object(ScientificWorkerConfig, "__call__") as load:
            self.assertEqual(main(), 0)
        serve.assert_called_once_with(Path("/run/secs/worker/worker.sock"), ScientificWorkerConfig("/checkpoint", "/input/molformer.lock.toml"))
        load.assert_not_called()

    def test_supervisor_failure_keeps_stop_uncertainty_and_safe_cause(self):
        error = WorkerStopUnconfirmed("The scientific child has not exited after termination; the supervisor cannot accept another session")
        error.__cause__ = OSError(errno.EIO, "private-cause")
        output = StringIO()
        with patch.object(Path, "read_bytes", return_value=CONFIG), patch("sys.stderr", output), \
             patch("secs_inference.provider.worker.serve_worker", side_effect=error):
            self.assertEqual(main(), 1)
        text = output.getvalue()
        self.assertIn("running the scientific worker supervisor", text)
        self.assertIn("has not exited", text)
        self.assertNotIn("private-cause", text)
        self.assertEqual(json.loads(text.splitlines()[1])["cause"]["reason"], "Input/output error")
