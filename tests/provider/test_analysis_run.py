"""Acquisition outages and observed membership changes have different owners."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from secs_inference.provider.analysis_run import AttemptSources, AcquiredUpload, UploadSetChanged, _worker_request, run_analysis
from secs_inference.provider.chat import InterpreterError
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.job_input import JobSpecification
from secs_inference.provider.job_api import ApiError, ApiUnavailable, AttemptSnapshot
from secs_inference.provider.execution import AnalysisCancelled, AttemptNoLongerActive, ExecutionLoop
from secs_inference.provider.job_upload import JobUpload, UploadReadCapability
from secs_inference.provider.source_access import InputReadError
from secs_inference.provider.worker import WorkerError, WorkerStopUnconfirmed
from test_interpreter import ScriptedChat, tool
from test_execution import FakeApi, ACTIVE, START


UPLOAD = JobUpload("upload:sha256:" + "a" * 64, "Proton experiment", 4, None)
GRANT = UploadReadCapability(UPLOAD.upload_ref, 4, "sha256:" + "b" * 64,
                             datetime(2099, 1, 1, tzinfo=timezone.utc), "https://store.test/bytes", "secret")


class AcquisitionTests(unittest.TestCase):
    def test_worker_response_and_failed_stop_both_survive_without_releasing_sources(self):
        for response in ({"outcome": "failed", "exception_type": "ValueError", "frames": []}, {"outcome": "unknown"}):
            with self.subTest(outcome=response["outcome"]), TemporaryDirectory() as directory:
                root = Path(directory)
                api = FakeApi()
                worker = Mock()
                worker.request.return_value = response
                stopped = WorkerStopUnconfirmed("Scientific worker exit could not be confirmed")
                worker.stop.side_effect = stopped
                def analyse(active):
                    with AttemptSources(api, active, (), None, root / "current", deadline=monotonic() + 5,
                                        max_total_bytes=100) as sources:
                        return _worker_request(worker, sources, {"operation": "analyse"}, monotonic() + 5)
                with AttemptStore(root / "journal") as journal:
                    journal.save(START)
                    with self.assertRaises(WorkerStopUnconfirmed) as caught:
                        ExecutionLoop(api, journal, analyse, journal.diagnose).step()
                    self.assertIs(caught.exception, stopped)
                    self.assertEqual(journal.load(), ACTIVE)
                self.assertTrue((root / "current").exists())
                self.assertFalse(any(isinstance(call, bytes) for call in api.calls))
                evidence = json.loads(next((root / "journal").glob("*.diagnostic.json")).read_bytes())
                self.assertEqual(evidence["context"]["exception_type"], "WorkerError")
                if response["outcome"] == "failed":
                    self.assertEqual(evidence["context"]["worker"], response)
                else:
                    self.assertIn("unrecognized operation outcome", evidence["context"]["message"])
                worker.stop.assert_called_once()

    def test_worker_errors_name_inspection_or_search_in_the_published_failure(self):
        for operation, description in (("inspect", "input inspection"), ("analyse", "SECS structure search")):
            for failure in ({"outcome": "failed", "exception_type": "ValueError", "frames": []},
                            WorkerError("The scientific worker disconnected; its exit was confirmed"),
                            TimeoutError("private timeout text")):
                with self.subTest(operation=operation, failure=type(failure).__name__), TemporaryDirectory() as directory:
                    api = FakeApi()
                    worker = Mock()
                    worker.request.side_effect = [failure]
                    sources = SimpleNamespace(api=api, active=None, acquired={}, directory=Path(directory))
                    with AttemptStore(Path(directory) / "journal") as journal:
                        ExecutionLoop(api, journal, lambda _: _worker_request(
                            worker, sources, {"operation": operation}, monotonic() + 10,
                        ), journal.diagnose).step()
                    result = json.loads(api.calls[-1])
                    self.assertIn(description, result["failure_message"])
                    if isinstance(failure, TimeoutError):
                        self.assertEqual(result["failure_code"], "work_deadline_exceeded")
                        self.assertIn("scientific worker was stopped", result["failure_message"])
                    else:
                        self.assertEqual(result["failure_code"], "scientific_execution_failed")
                    self.assertNotIn("private timeout text", result["failure_message"])

    def test_cleanup_failure_does_not_discard_completed_analysis(self):
        api = Mock()
        api.specification.return_value = JobSpecification("job:test", "C2H6O")
        api.uploads.return_value = (UPLOAD,)
        api.capability.return_value = GRANT
        chat = ScriptedChat(tool("read_jcamp", {"source": {"upload_ref": UPLOAD.upload_ref, "member": None},
            "formula": "C2H6O", "explanation": "Proton experiment."}))
        worker = Mock()
        worker.request.return_value = {"outcome": "analysed", "analysis": {"candidates": []}}
        with TemporaryDirectory() as directory, patch("secs_inference.provider.analysis_run.download_upload",
                return_value=Path(directory) / "verified"):
            with self.assertLogs("secs_inference.provider.analysis_run", level="ERROR"), patch(
                    "secs_inference.provider.analysis_run.shutil.rmtree", side_effect=PermissionError("private path")):
                report = run_analysis(api=api, active=None, chat=chat, worker=worker, store=None,
                    directory=Path(directory) / "current", work_deadline=monotonic() + 10,
                    interpretation_seconds=5, max_turns=1, max_total_bytes=100)
            self.assertTrue((Path(directory) / "current").exists())
        self.assertEqual(report["outcome"], "analysed")
        self.assertEqual(report["analysis"], worker.request.return_value["analysis"])
        self.assertTrue(report["input_choices"][0]["used"])

    def test_worker_failures_keep_analysis_evidence_without_reclassifying_the_failure(self):
        for failure, code in (({"outcome": "failed", "exception_type": "ValueError", "frames": []}, "scientific_execution_failed"),
                              (TimeoutError("work expired"), "work_deadline_exceeded")):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                root = Path(directory)
                api = FakeApi()
                api.specification = Mock(return_value=JobSpecification("job:test", "C2H6O"))
                api.uploads = Mock(return_value=(UPLOAD,))
                api.capability = Mock(return_value=GRANT)
                chat = ScriptedChat(tool("read_jcamp", {}), tool("read_jcamp", {
                    "source": {"upload_ref": UPLOAD.upload_ref, "member": "chosen.jdx"},
                    "formula": "C2H6O", "explanation": "Proton experiment."}))
                worker = Mock()
                worker.request.side_effect = [failure]
                with AttemptStore(root / "journal") as journal, patch(
                        "secs_inference.provider.analysis_run.download_upload", return_value=root / "verified"):
                    def analyse(active):
                        return run_analysis(api=api, active=active, chat=chat, worker=worker, store=None,
                            directory=root / "current", work_deadline=monotonic() + 10,
                            interpretation_seconds=5, max_turns=3, max_total_bytes=100)
                    ExecutionLoop(api, journal, analyse, journal.diagnose).step()
                    self.assertEqual(json.loads(api.calls[-1])["failure_code"], code)
                    diagnostic = json.loads((journal.directory / ("b" * 64 + ".diagnostic.json")).read_bytes())
                evidence = diagnostic["analysis"]
                self.assertEqual(evidence["input_choices"][0]["source"]["member"], "chosen.jdx")
                self.assertEqual(evidence["input_choices"][0]["formula"], "C2H6O")
                self.assertEqual(evidence["interpretation_rejections"][0]["stage"], "tool_call")
                self.assertEqual(evidence["acquired_uploads"][UPLOAD.upload_ref]["content_hash"], GRANT.content_hash)
                if isinstance(failure, dict):
                    self.assertEqual(diagnostic["worker"], failure)

    def test_reader_rejection_survives_exhausted_interpretation_budget(self):
        api = Mock()
        api.specification.return_value = JobSpecification("job:test", "C21H22N2O2")
        api.uploads.return_value = (UPLOAD,)
        api.capability.return_value = GRANT
        chat = ScriptedChat(tool("read_bruker", {"upload_ref": UPLOAD.upload_ref,
            "pdata_directory": "pdata/1", "formula": "C21H22N2O2", "explanation": "Proton."}))
        worker = Mock()
        worker.request.return_value = {"outcome": "input_rejected", "reason": "Cannot decode the processed spectrum."}
        with TemporaryDirectory() as directory, patch("secs_inference.provider.analysis_run.download_upload",
                return_value=Path(directory) / "verified"):
            with self.assertRaises(InterpreterError) as caught:
                run_analysis(api=api, active=None, chat=chat, worker=worker, store=None,
                    directory=Path(directory) / "current", work_deadline=monotonic() + 10,
                    interpretation_seconds=5, max_turns=1, max_total_bytes=100)
        evidence = caught.exception.analysis_context
        self.assertEqual(evidence["input_choices"][0]["reading_error"], worker.request.return_value["reason"])
        self.assertEqual(evidence["interpretation_rejections"], [])
        self.assertEqual(evidence["acquired_uploads"][UPLOAD.upload_ref]["content_hash"], GRANT.content_hash)

    def test_invalid_bruker_call_is_evidence_not_a_reader_failure(self):
        for explains in (True, False):
            with self.subTest(explains=explains), TemporaryDirectory() as directory:
                api = Mock()
                api.specification.return_value = JobSpecification("job:test", "C21H22N2O2")
                api.uploads.return_value = (UPLOAD,)
                malformed = tool("read_bruker", {"source": {"upload_ref": UPLOAD.upload_ref, "member": "pdata/1"},
                    "formula": "C21H22N2O2", "explanation": "Proton."})
                chat = ScriptedChat(malformed, tool("report_input_problem", {"explanation": "The reader rejected the directory."}))
                worker = Mock()
                def run():
                    return run_analysis(api=api, active=None, chat=chat, worker=worker, store=None,
                        directory=Path(directory) / "current", work_deadline=monotonic() + 10,
                        interpretation_seconds=5, max_turns=2 if explains else 1, max_total_bytes=100)
                if explains:
                    report = run()
                    self.assertEqual(report["input_choices"], [])
                    evidence = report["interpretation_rejections"]
                else:
                    with self.assertRaises(InterpreterError) as caught:
                        run()
                    evidence = caught.exception.analysis_context["interpretation_rejections"]
                self.assertEqual(evidence[0]["stage"], "tool_call")
                self.assertIn("upload_ref, pdata_directory, formula, explanation", evidence[0]["reason"])
                worker.request.assert_not_called()
                api.capability.assert_not_called()

    def test_inspection_budget_feedback_explains_why_a_second_upload_was_not_downloaded(self):
        second = JobUpload("upload:sha256:" + "c" * 64, "Other experiment", 4, None)
        api = Mock()
        api.specification.return_value = JobSpecification("job:test", "Find the proton spectrum, C2H6O")
        api.uploads.return_value = (UPLOAD, second)
        api.capability.return_value = GRANT
        chat = ScriptedChat(
            tool("inspect_source", {"source": {"upload_ref": UPLOAD.upload_ref, "member": None}}),
            tool("read_jcamp", {"source": {"upload_ref": second.upload_ref, "member": "proton.jdx"},
                               "formula": "C2H6O", "explanation": "The second experiment is proton."}),
            tool("report_input_problem", {"explanation": "The remaining input cannot be obtained within the provider's allowance."}),
        )
        worker = Mock()
        worker.request.return_value = {"outcome": "inspected", "facts": {}}
        with TemporaryDirectory() as directory, patch("secs_inference.provider.analysis_run.download_upload",
                                                       return_value=Path(directory) / "verified") as download:
            report = run_analysis(api=api, active=None, chat=chat, worker=worker, store=None,
                                  directory=Path(directory) / "current", work_deadline=monotonic() + 10,
                                  interpretation_seconds=5, max_turns=3, max_total_bytes=6)
        feedback = chat.requests[-1][-1]["content"]
        for fact in ("4-byte Upload was not downloaded", "2 bytes remain", "inspection downloads", "6-byte allowance", "cannot be discarded", "whole Upload"):
            self.assertIn(fact, feedback)
        self.assertEqual(report["input_choices"][0]["reading_error"], feedback)
        api.capability.assert_called_once()
        download.assert_called_once()

    def test_cleanup_failure_is_logged_without_replacing_the_analysis_error(self):
        with TemporaryDirectory() as directory:
            failure = RuntimeError("original analysis failure")
            with self.assertLogs("secs_inference.provider.analysis_run", level="ERROR") as logged:
                with self.assertRaises(RuntimeError) as caught, patch(
                        "secs_inference.provider.analysis_run.shutil.rmtree", side_effect=PermissionError("secret path")):
                    with AttemptSources(None, None, (), None, Path(directory) / "current",
                                        deadline=monotonic() + 1, max_total_bytes=100):
                        raise failure
            self.assertIs(caught.exception, failure)
            self.assertIn("cleanup is required before another Attempt", " ".join(logged.output))
            self.assertNotIn("secret path", " ".join(logged.output))

    def test_retry_never_starts_after_deadline_and_expiry_precedes_retry_exhaustion(self):
        for expires_after in (1, 3):
            with self.subTest(expires_after=expires_after), TemporaryDirectory() as directory:
                now = [0]
                api = Mock()
                calls = []
                def capability(*args):
                    calls.append(True)
                    if len(calls) == expires_after and expires_after == 3:
                        now[0] = 10
                    raise ApiUnavailable("retryable outage")
                api.capability.side_effect = capability
                def sleep(_seconds):
                    if expires_after == 1:
                        now[0] = 10
                with patch("secs_inference.provider.analysis_run.monotonic", side_effect=lambda: now[0]), patch(
                        "secs_inference.provider.analysis_run.sleep", side_effect=sleep):
                    with AttemptSources(api, None, (UPLOAD,), None, Path(directory) / "current",
                                        deadline=10, max_total_bytes=100) as sources:
                        with self.assertRaises(TimeoutError):
                            sources.acquire(UPLOAD.upload_ref)
                self.assertEqual(len(calls), expires_after)

    def test_inspection_uses_its_own_deadline_and_analysis_keeps_the_work_budget(self):
        for inspection_failure in (None, TimeoutError("inspection expired"), WorkerStopUnconfirmed("no exit proof")):
            with self.subTest(failure=type(inspection_failure).__name__), TemporaryDirectory() as directory:
                api = Mock()
                api.specification.return_value = JobSpecification("job:test", "Use the proton experiment, C2H6O")
                api.uploads.return_value = (UPLOAD,)
                api.capability.return_value = GRANT
                chat = ScriptedChat(
                    tool("inspect_source", {"source": {"upload_ref": UPLOAD.upload_ref, "member": None}}),
                    tool("read_jcamp", {"source": {"upload_ref": UPLOAD.upload_ref, "member": None},
                                       "formula": "C2H6O", "explanation": "Proton experiment."}),
                )
                worker = Mock()
                worker.request.side_effect = ([inspection_failure] if inspection_failure else
                                               [{"outcome": "inspected", "facts": {}}, {"outcome": "analysed", "analysis": {}}])
                work_deadline = monotonic() + 1800
                with patch("secs_inference.provider.analysis_run.download_upload", return_value=Path(directory) / "verified") as download:
                    def run():
                        return run_analysis(api=api, active=None, chat=chat, worker=worker, store=None,
                                            directory=Path(directory) / "current", work_deadline=work_deadline,
                                            interpretation_seconds=120, max_turns=3, max_total_bytes=100)
                    if inspection_failure:
                        expected = WorkerStopUnconfirmed if isinstance(inspection_failure, WorkerStopUnconfirmed) else InterpreterError
                        with self.assertRaises(expected):
                            run()
                    else:
                        self.assertEqual(run()["outcome"], "analysed")
                        self.assertEqual(worker.request.call_args_list[1].kwargs["deadline"], work_deadline)
                    inspection_deadline = worker.request.call_args_list[0].kwargs["deadline"]
                    self.assertLess(inspection_deadline, work_deadline)
                    self.assertEqual(download.call_args.kwargs["deadline"], inspection_deadline)
                    self.assertEqual((Path(directory) / "current").exists(), isinstance(inspection_failure, WorkerStopUnconfirmed))

    def test_unconfirmed_stop_retains_sources_but_confirmed_timeout_releases_them(self):
        for failure in (WorkerStopUnconfirmed("No stop receipt"), TimeoutError("Stopped")):
            with self.subTest(failure=type(failure)), TemporaryDirectory() as directory:
                root = Path(directory) / "current"
                with self.assertRaises(type(failure)):
                    with AttemptSources(None, None, (), None, root, deadline=monotonic() + 1, max_total_bytes=100):
                        (root / "input").write_bytes(b"source")
                        raise failure
                self.assertEqual(root.exists(), isinstance(failure, WorkerStopUnconfirmed))

    def test_reconciliation_outage_shares_the_existing_acquisition_budget(self):
        for recovers in (True, False):
            with self.subTest(recovers=recovers), TemporaryDirectory() as directory:
                api = Mock()
                api.capability.side_effect = ([ApiError("missing", status=404), GRANT] if recovers else
                                               [ApiError("missing", status=404)] * 3)
                api.uploads.side_effect = ApiUnavailable("temporarily unavailable")
                with AttemptSources(api, None, (UPLOAD,), None, Path(directory) / "current",
                                    deadline=monotonic() + 10, max_total_bytes=100) as sources:
                    with patch("secs_inference.provider.analysis_run.sleep"), patch(
                            "secs_inference.provider.analysis_run.download_upload", return_value=sources.directory / "verified") as download:
                        if recovers:
                            sources.acquire(UPLOAD.upload_ref)
                            sources.acquire(UPLOAD.upload_ref)
                            self.assertEqual(api.capability.call_count, 2)
                            self.assertEqual(api.uploads.call_count, 1)
                            download.assert_called_once()
                            self.assertEqual(sources.acquired[UPLOAD.upload_ref].content_hash, GRANT.content_hash)
                        else:
                            with self.assertRaises(ApiUnavailable):
                                sources.acquire(UPLOAD.upload_ref)
                            self.assertEqual(api.capability.call_count, 3)
                            self.assertEqual(api.uploads.call_count, 3)
                            download.assert_not_called()

    def test_observed_removal_reaches_interpreter_and_invalidates_cached_selection(self):
        second = JobUpload("upload:sha256:" + "c" * 64, "Other experiment", 4, None)
        api = Mock()
        api.capability.side_effect = ApiError("missing", status=404)
        api.uploads.return_value = ()
        with TemporaryDirectory() as directory:
            with AttemptSources(api, None, (UPLOAD, second), None, Path(directory) / "current",
                                deadline=monotonic() + 10, max_total_bytes=100) as sources:
                sources.acquired[UPLOAD.upload_ref] = AcquiredUpload(sources.directory / "verified", 4, GRANT.content_hash)
                with self.assertRaises(UploadSetChanged) as change:
                    sources.acquire(second.upload_ref)
                self.assertEqual(change.exception.uploads, ())
                with self.assertRaisesRegex(InputReadError, "not in that list"):
                    sources.acquire(UPLOAD.upload_ref)
                api.capability.assert_called_once()

    def test_missed_lifecycle_poll_does_not_cancel_work_but_observed_cancel_does(self):
        api = Mock()
        sources = SimpleNamespace(api=api, active=None, acquired={}, directory=Path("/private/current"))
        class Worker:
            def request(self, command, *, deadline, check_active):
                check_active()
                return {"outcome": "inspected", "facts": {}}
        api.snapshot.side_effect = ApiUnavailable("outage")
        self.assertEqual(_worker_request(Worker(), sources, {"operation": "inspect"}, monotonic() + 5)["outcome"], "inspected")
        api.snapshot.side_effect = None
        api.snapshot.return_value = AttemptSnapshot("in_progress", "cancelled")
        with self.assertRaisesRegex(AnalysisCancelled, "cancelled"):
            _worker_request(Worker(), sources, {"operation": "inspect"}, monotonic() + 5)
        api.snapshot.return_value = AttemptSnapshot("expired", "open")
        with self.assertRaisesRegex(AttemptNoLongerActive, "already expired"):
            _worker_request(Worker(), sources, {"operation": "inspect"}, monotonic() + 5)
