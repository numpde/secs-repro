"""Operator records distinguish remote effects from local durability."""

import errno
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.attempt_state import ActiveAttempt, StartPending, TerminalPending
from secs_inference.provider.attempt_store import AttemptStore, JournalError
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.job_api import complete_command
from test_execution import ACTIVE, START, REPORT, FakeApi


class LifecycleReportingTests(unittest.TestCase):
    def test_storage_failures_do_not_claim_later_effects(self):
        for failed_state in (StartPending, ActiveAttempt, TerminalPending):
            with self.subTest(failed_state=failed_state.__name__), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    api = FakeApi()
                    readiness = Mock()
                    save = journal.save
                    def fail_state(state):
                        if isinstance(state, failed_state):
                            with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=OSError(errno.EIO, "private-disk")):
                                return save(state)
                        return save(state)
                    with patch.object(journal, "save", side_effect=fail_state), \
                         patch("secs_inference.provider.execution._LOG") as log:
                        with self.assertRaises(JournalError):
                            ExecutionLoop(api, journal, lambda _: REPORT, journal.diagnose, readiness).step()
                    # Render logging arguments just as a handler does, without
                    # requiring a first log when the initial durable save fails.
                    text = "\n".join(call.args[0] % call.args[1:] for call in log.info.call_args_list)
                    self.assertNotIn("sending retained complete command", text)
                    self.assertNotIn("publication confirmed", text)
                    if failed_state is StartPending:
                        readiness.assert_not_called()
                        self.assertEqual(api.calls, ["feed"])
                        self.assertEqual(text, "")
                    elif failed_state is ActiveAttempt:
                        self.assertIn(ACTIVE.execution_attempt_ref, text)
                        self.assertIn("API confirmed", text)
                        self.assertNotIn("active state retained", text)
                    else:
                        self.assertIn("active state retained", text)
                    self.assertNotIn("private-disk", text)

    def test_publication_confirmation_survives_failed_local_retirement(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            terminal = complete_command(ACTIVE, REPORT)
            journal.save(terminal)
            api = FakeApi()
            readiness = Mock()
            with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=OSError(errno.EIO, "private-disk")), \
                 self.assertLogs("secs_inference.provider.execution", level="INFO") as captured:
                with self.assertRaises(JournalError):
                    ExecutionLoop(api, journal, lambda _: self.fail("Analysis replayed"), journal.diagnose, readiness).step()
            text = "\n".join(captured.output)
            self.assertIn(ACTIVE.execution_attempt_ref, text)
            self.assertIn("complete publication confirmed", text)
            self.assertNotIn("journal retirement confirmed", text)
            readiness.assert_not_called()
            self.assertEqual(api.calls, [terminal.body])

    def test_readiness_failure_keeps_recovered_start_identity_without_admission_claim(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            journal.save(START)
            api = FakeApi()
            def unavailable(start):
                self.assertEqual(start, START)
                raise RuntimeError("private-readiness")
            with self.assertLogs("secs_inference.provider.execution", level="INFO") as captured:
                with self.assertRaises(RuntimeError):
                    ExecutionLoop(api, journal, lambda _: REPORT, journal.diagnose, unavailable).step()
            text = "\n".join(captured.output)
            self.assertIn("recovering retained start intent logical-start", text)
            self.assertIn(START.selected.job_ref, text)
            self.assertNotIn("sending retained start", text)
            self.assertEqual(api.calls, [])
            self.assertEqual(journal.load(), START)
