"""Storage uncertainty halts API effects without erasing earlier failure evidence."""

import errno
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from secs_inference.provider.attempt_store import AttemptStore, JournalError, provider_error_details
from secs_inference.provider.diagnostics import exception_evidence
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.worker import WorkerStopUnconfirmed
from test_execution import ACTIVE, START, REPORT, FakeApi


class JournalFailureTests(unittest.TestCase):
    def test_staging_sync_failure_preserves_previous_record_and_stops_effects(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            store.save(START)
            original = (store.directory / "attempt.json").read_bytes()
            with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=OSError(errno.ENOSPC, "private")):
                with self.assertRaises(JournalError) as caught:
                    store.save(ACTIVE)
            self.assertIn("writing and syncing the staging file", str(caught.exception))
            self.assertIn("No space left", str(caught.exception))
            self.assertEqual((store.directory / "attempt.json").read_bytes(), original)
            self.assertEqual(list(store.directory.glob(".attempt-*")), [])
            api = FakeApi()
            with self.assertRaises(JournalError):
                ExecutionLoop(api, store, lambda _: REPORT, store.diagnose).step()
            self.assertEqual(api.calls, [])

    def test_directory_sync_failure_does_not_claim_replacement_or_retirement_durability(self):
        for operation in ("save", "clear"):
            with self.subTest(operation=operation), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store:
                    store.save(START)
                    fsync = os.fsync
                    def fail_directory_sync(descriptor):
                        if descriptor == store._directory_fd:
                            raise OSError(errno.EIO, "private")
                        fsync(descriptor)
                    with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=fail_directory_sync):
                        with self.assertRaises(JournalError) as caught:
                            store.save(ACTIVE) if operation == "save" else store.clear()
                    self.assertIn("syncing the journal directory", str(caught.exception))
                    self.assertIn("unconfirmed", str(caught.exception))
                    self.assertNotIn("record remains", str(caught.exception))
                    self.assertNotIn("private", str(caught.exception))
                    with self.assertRaises(JournalError):
                        store.load()

    def test_evidence_sync_failure_preserves_analysis_cause_and_active_obligation(self):
        for outcome in ("exception", "cannot_analyse"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                journal_path = Path(directory) / "journal"
                api = FakeApi()
                with AttemptStore(journal_path) as store:
                    store.save(START)
                    original_sync = os.fsync
                    evidence_started = False
                    def analyse(_):
                        nonlocal evidence_started
                        evidence_started = True
                        if outcome == "exception":
                            raise ValueError("private-analysis")
                        return REPORT | {"outcome": outcome, "explanation": "Missing spectrum."}
                    def fail_evidence_sync(descriptor):
                        if evidence_started:
                            raise OSError(errno.ENOSPC, "private-storage")
                        original_sync(descriptor)
                    with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=fail_evidence_sync):
                        with self.assertRaises(JournalError) as caught:
                            ExecutionLoop(api, store, analyse, store.diagnose).step()
                    self.assertIn("settlement has stopped", str(caught.exception))
                    evidence = exception_evidence(caught.exception, boundary_details=provider_error_details)
                    self.assertEqual(evidence["cause"]["errno"], errno.ENOSPC)
                    if outcome == "exception":
                        self.assertEqual(evidence["cause"]["context"]["exception_type"], "ValueError")
                    self.assertNotIn("private-", json.dumps(evidence))
                    self.assertFalse(any(isinstance(call, bytes) for call in api.calls))
                with AttemptStore(journal_path) as recovered:
                    self.assertEqual(recovered.load(), ACTIVE)

    def test_unconfirmed_stop_keeps_original_context_when_diagnostic_storage_also_fails(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            store.save(START)
            api = FakeApi()
            stop = WorkerStopUnconfirmed("Worker exit was not confirmed.")
            primary = EOFError("private-worker-reply")
            def analyse(_):
                try:
                    raise primary
                except EOFError:
                    raise stop from None
            with patch.object(store, "diagnose", side_effect=OSError(errno.ENOSPC, "private-storage")):
                with self.assertLogs("secs_inference.provider.execution", level="ERROR") as captured:
                    with self.assertRaises(WorkerStopUnconfirmed) as caught:
                        ExecutionLoop(api, store, analyse, store.diagnose).step()
            self.assertIs(caught.exception, stop)
            self.assertIs(stop.__context__, primary)
            self.assertEqual(store.load(), ACTIVE)
            self.assertFalse(any(isinstance(call, bytes) for call in api.calls))
            rendered = "\n".join(captured.output)
            self.assertIn(ACTIVE.execution_attempt_ref, rendered)
            self.assertIn(str(errno.ENOSPC), rendered)
            self.assertNotIn("private-", rendered)

    def test_staging_cleanup_failure_is_visible_without_replacing_storage_failure(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=OSError(errno.ENOSPC, "private-write")), \
                 patch("secs_inference.provider.attempt_store.Path.unlink", side_effect=OSError(errno.EACCES, "private-cleanup")):
                with self.assertLogs("secs_inference.provider.attempt_store", level="ERROR") as captured:
                    with self.assertRaises(JournalError) as caught:
                        store.save(START)
            self.assertEqual(caught.exception.__cause__.errno, errno.ENOSPC)
            self.assertIn("staging file", "\n".join(captured.output))
            self.assertIn(str(errno.EACCES), "\n".join(captured.output))
            self.assertNotIn("private-", "\n".join(captured.output))
