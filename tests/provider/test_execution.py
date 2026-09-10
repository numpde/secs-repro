"""Retained API obligations, not process memory, govern inference recovery."""

from dataclasses import replace
import json
from hashlib import sha256
from base64 import b64decode
from pathlib import Path
import os
import socket
import ssl
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.attempt_state import ActiveAttempt, StartPending, TerminalPending
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import ExecutionLoop, AnalysisCancelled, AttemptNoLongerActive
from secs_inference.provider.job_api import ApiError, ApiUnavailable, AttemptSnapshot, JobApi, complete_command, fail_command
from secs_inference.provider.job_input import SelectedJobInput
from secs_inference.provider.http import HttpsEndpoint, HttpResponse, RequestDelivery, RequestUnavailable, ResponseRejected, ResponseRejection, TlsRejected


START = StartPending("provider:test", SelectedJobInput("job:test", "nmr.job.specification.text.v1", "sha256:" + "a" * 64, 4), "logical-start")
ACTIVE = ActiveAttempt(START, "execution_attempt:sha256:" + "b" * 64)
REPORT = {"schema_id": "secs.elucidation.result.v1", "outcome": "analysed", "analysis": {}}


class FakeApi:
    provider_ref = START.provider_ref
    def __init__(self):
        self.start_state = "in_progress"
        self.fail_publication = False
        self.calls = []
    def next_job(self):
        self.calls.append("feed")
        return START.selected
    def start(self, start):
        self.calls.append(start)
        return ActiveAttempt(start, ACTIVE.execution_attempt_ref), self.start_state
    def snapshot(self, active):
        self.calls.append("snapshot")
        return AttemptSnapshot("in_progress", "open")
    def publish(self, terminal):
        self.calls.append(terminal.body)
        if self.fail_publication:
            raise ApiUnavailable("Response lost")


class ExecutionTests(unittest.TestCase):
    def test_unknown_timeout_text_is_not_published_as_owned_deadline_evidence(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            api = FakeApi()
            def analyse(_):
                raise TimeoutError("private arbitrary timeout detail")
            ExecutionLoop(api, journal, analyse, journal.diagnose).step()
        result = json.loads(api.calls[-1])
        self.assertEqual(result["failure_code"], "provider_execution_failed")
        self.assertIn("internal operation timed out", result["failure_message"])
        self.assertNotIn("work deadline", result["failure_message"])
        self.assertNotIn("private", result["failure_message"])
        self.assertNotIn("worker was stopped", result["failure_message"])

    def test_api_transport_failure_keeps_cause_and_delivery_without_private_text(self):
        transport = ProviderApi(HttpsEndpoint("https://api.test", "web", 1, 1), START.provider_ref,
                               "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
        for delivery, cause, reason in (
            (RequestDelivery.NOT_SENT, socket.gaierror(-2, "private hostname"), "address could not be resolved"),
            (RequestDelivery.POSSIBLE, ConnectionResetError("private URL"), "connection was reset"),
        ):
            with self.subTest(delivery=delivery), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store, patch(
                        "secs_inference.provider.api.send_provider_request",
                        return_value=RequestUnavailable(delivery, cause)):
                    api = FakeApi()
                    ExecutionLoop(api, store, JobApi(transport).specification, store.diagnose).step()
                result = json.loads(api.calls[-1])
                self.assertIn("read the Job specification", result["failure_message"])
                self.assertIn(reason, result["failure_message"])
                self.assertIn("not sent" if delivery is RequestDelivery.NOT_SENT else "outcome is unknown", result["failure_message"])
                retained = json.loads(next((Path(directory) / "journal").glob("*.diagnostic.json")).read_bytes())
                self.assertEqual(retained["api"]["delivery"], delivery.value)
                self.assertEqual(retained["api"]["exception_type"], type(cause).__name__)
                self.assertNotIn("worker", retained)
                self.assertNotIn("private", json.dumps(retained))
                self.assertNotIn("private", result["failure_message"])

    def test_attempt_snapshot_accepts_a_read_reply_and_rejects_other_operations(self):
        transport = Mock(provider_ref=START.provider_ref)
        api = JobApi(transport)
        response = {
            "schema_id": "nmr.provider.execution_attempt_read_response.v1",
            "execution_attempt_ref": ACTIVE.execution_attempt_ref,
            "job_ref": START.selected.job_ref,
            "state": "in_progress", "job_state": "open",
        }
        transport.request.return_value = json.dumps(response).encode()
        self.assertEqual(api.snapshot(ACTIVE), AttemptSnapshot("in_progress", "open"))

        response["schema_id"] = "nmr.provider.execution_attempt_start_response.v1"
        transport.request.return_value = json.dumps(response).encode()
        with self.assertRaisesRegex(ApiError, "response schema differs") as caught:
            api.snapshot(ACTIVE)
        self.assertIn("nmr.provider.execution_attempt_read_response.v1", str(caught.exception))

    def test_inability_is_failed_with_private_evidence_and_identical_publication_replay(self):
        report = {"schema_id": REPORT["schema_id"], "outcome": "cannot_analyse",
                  "explanation": "The reader rejected the input.", "input_choices": [],
                  "interpretation_rejections": [{"stage": "tool_call", "reason": "Missing formula argument."}]}
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            api = FakeApi()
            analyse = Mock(return_value=report)
            loop = ExecutionLoop(api, store, analyse, store.diagnose)
            api.fail_publication = True
            with self.assertRaises(ApiUnavailable):
                loop.step()
            terminal = store.load()
            self.assertEqual(terminal.operation, "fail")
            body = json.loads(terminal.body)
            self.assertEqual(body["failure_code"], "cannot_analyse")
            self.assertIn("Interpreter explanation: " + report["explanation"], body["failure_message"])
            self.assertNotIn("canonical_result_base64", body)
            evidence = store.directory / ("b" * 64 + ".report.json")
            self.assertEqual(json.loads(evidence.read_bytes()), report)
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            api.fail_publication = False
            loop.step()
            analyse.assert_called_once()
            self.assertEqual(api.calls[-2:], [terminal.body, terminal.body])

    def test_inability_evidence_write_failure_does_not_publish_or_retire_active_work(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            api = FakeApi()
            report = REPORT | {"outcome": "cannot_analyse", "explanation": "Missing spectrum."}
            with patch.object(store, "record_report", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ExecutionLoop(api, store, lambda _: report, store.diagnose).step()
            self.assertIsInstance(store.load(), ActiveAttempt)
            self.assertFalse(any(isinstance(call, bytes) for call in api.calls))

    def test_oversize_report_explains_the_limit_but_unknown_errors_stay_private(self):
        for report in ({"text": "x" * 786433}, {"bug": object()}):
            with self.subTest(oversize="text" in report), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store:
                    api = FakeApi()
                    ExecutionLoop(api, store, lambda _: report, lambda *_: None).step()
                    command = json.loads(api.calls[-1])
                    if "text" in report:
                        self.assertEqual(command["failure_code"], "api_access_failed")
                        self.assertIn("786432-byte API result limit", command["failure_message"])
                    else:
                        self.assertEqual(command["failure_code"], "provider_execution_failed")
                        self.assertNotIn("object", command["failure_message"])

    def test_attempt_failures_retain_owned_api_evidence_without_remote_text(self):
        transport = ProviderApi(HttpsEndpoint("https://api.test", "web", 1, 1), START.provider_ref,
                                "credential:test", Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
        jobs = JobApi(transport)
        cases = (
            (HttpResponse(200, None, b"secret malformed JSON"), jobs.specification, "selected Job input"),
            (HttpResponse(200, None, b"secret malformed JSON"), jobs.uploads, "unreadable JSON"),
            (TlsRejected(ssl.SSLCertVerificationError("secret certificate")), jobs.specification, "TLS certificate could not be verified; the request was not sent"),
            (TlsRejected(ssl.SSLError("secret protocol")), jobs.specification, "encrypted connection failed; the request was not sent"),
            (ResponseRejected(ResponseRejection.INVALID_CONTENT_TYPE, 200), jobs.specification, "Content-Type does not identify the required JSON response"),
            (RequestUnavailable(RequestDelivery.POSSIBLE, OSError("secret transport detail")), jobs.specification, "request may have reached the API"),
            (RequestUnavailable(RequestDelivery.RESPONSE_RECEIVED, status=502), jobs.specification, "HTTP 502 did not yield an admitted API response"),
            (HttpResponse(503, "request-test", b"secret response"), jobs.specification, "HTTP 503 for request request-test"),
            (HttpResponse(404, "request-test", b"secret invalid problem"), jobs.specification, "HTTP 404 for request request-test"),
        )
        for response, analyse, evidence in cases:
            with self.subTest(evidence=evidence), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store, patch(
                        "secs_inference.provider.api.send_provider_request", return_value=response):
                    api = FakeApi()
                    ExecutionLoop(api, store, analyse, lambda *_: None).step()
                    command = json.loads(api.calls[-1])
                    self.assertEqual(command["failure_code"], "api_access_failed")
                    self.assertIn(evidence, command["failure_message"])
                    self.assertNotIn("secret", command["failure_message"])
                    self.assertIsNone(store.load())

    def test_observed_policy_stops_do_not_become_scientific_failures(self):
        for failure in (AnalysisCancelled(), AttemptNoLongerActive("expired")):
            with self.subTest(outcome=type(failure).__name__), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store:
                    api = FakeApi()
                    def analyse(active):
                        raise failure
                    ExecutionLoop(api, store, analyse, lambda *_: self.fail("Policy stop needs no bug diagnostic")).step()
                    self.assertIsNone(store.load())
                    publications = [call for call in api.calls if isinstance(call, bytes)]
                    if isinstance(failure, AnalysisCancelled):
                        self.assertEqual(json.loads(publications[0])["failure_code"], "job_cancelled")
                    else:
                        self.assertEqual(publications, [])

    def test_non_regular_journal_is_rejected_without_waiting_for_a_writer(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            os.mkfifo(store.directory / "attempt.json", mode=0o600)
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                store.load()

    def test_worker_readiness_is_required_only_for_a_start_not_terminal_recovery(self):
        for retained in (START, ACTIVE, complete_command(ACTIVE, REPORT)):
            with self.subTest(state=type(retained).__name__), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store:
                    store.save(retained)
                    ready = []
                    ExecutionLoop(FakeApi(), store, lambda _: REPORT, lambda *_: None,
                                  lambda: ready.append(True)).step()
                    self.assertEqual(ready, [True] if retained is START else [])

    def test_lost_publication_replays_identical_bytes_without_reanalysing(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            api = FakeApi()
            analyses = []
            loop = ExecutionLoop(api, store, lambda active: analyses.append(active) or REPORT, lambda *_: None)
            api.fail_publication = True
            with self.assertRaises(ApiUnavailable):
                loop.step()
            retained = store.load()
            self.assertIsInstance(retained, TerminalPending)
            api.fail_publication = False
            loop.step()
            self.assertEqual(len(analyses), 1)
            self.assertEqual(api.calls[-2:], [retained.body, retained.body])
            self.assertIsNone(store.load())

    def test_restart_from_active_fails_interrupted_work_without_rerunning(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            store.save(ACTIVE)
            api = FakeApi()
            def no_analysis(active):
                self.fail("Interrupted inference was rerun")
            ExecutionLoop(api, store, no_analysis, lambda *_: None).step()
            command = json.loads(api.calls[-1])
            self.assertEqual(command["failure_code"], "provider_interrupted")
            self.assertIsNone(store.load())

    def test_terminal_start_replay_does_not_compute(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            store.save(START)
            api = FakeApi()
            api.start_state = "succeeded"
            with self.assertLogs("secs_inference.provider.execution", level="INFO") as logged:
                ExecutionLoop(api, store, lambda _: self.fail("Replayed terminal work"), lambda *_: None).step()
            self.assertEqual(api.calls, [START])
            self.assertIn(ACTIVE.execution_attempt_ref, " ".join(logged.output))
            self.assertIsNone(store.load())

    def test_expired_publication_keeps_attempt_identity_in_the_log_after_retirement(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            store.save(complete_command(ACTIVE, REPORT))
            api = FakeApi()
            api.publish = lambda _: (_ for _ in ()).throw(ApiError("conflict", status=409))
            api.snapshot = lambda _: AttemptSnapshot("expired", "closed")
            with self.assertLogs("secs_inference.provider.execution", level="WARNING") as logged:
                ExecutionLoop(api, store, lambda _: self.fail("Analysis was rerun"), lambda *_: None).step()
            self.assertIsNone(store.load())
            message = " ".join(logged.output)
            self.assertIn(ACTIVE.execution_attempt_ref, message)
            self.assertIn("Stopped retrying result publication", message)
            self.assertIn("the Attempt expired. Delivery was not confirmed", message)

    def test_other_provider_or_corrupt_start_record_stops_before_api_effects(self):
        for change in ("provider", "input"):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                root = Path(directory) / "journal"
                with AttemptStore(root) as store:
                    store.save(START)
                    if change == "provider":
                        store.save(replace(START, provider_ref="provider:other"))
                    else:
                        document = json.loads((root / "attempt.json").read_bytes())
                        document["selected"]["input_byte_length"] = "bad"
                        (root / "attempt.json").write_text(json.dumps(document))
                    api = FakeApi()
                    with self.assertRaises(RuntimeError):
                        ExecutionLoop(api, store, lambda _: REPORT, lambda *_: None).step()
                    self.assertEqual(api.calls, [])
                    self.assertTrue((root / "attempt.json").exists())

    def test_unconfirmed_durable_start_prevents_remote_start(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            api = FakeApi()
            loop = ExecutionLoop(api, store, lambda _: REPORT, lambda *_: None)
            with patch("secs_inference.provider.attempt_store.os.fsync", side_effect=OSError("disk failed")):
                with self.assertRaises(OSError):
                    loop.step()
            self.assertEqual(api.calls, ["feed"])
            with self.assertRaises(RuntimeError):
                store.load()
            self.assertFalse(any(store.directory.glob(".attempt-*")))

    def test_terminal_conflict_keeps_exact_evidence_instead_of_claiming_success(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as store:
            terminal = complete_command(ACTIVE, REPORT)
            store.save(terminal)
            api = FakeApi()
            api.publish = lambda _: (_ for _ in ()).throw(ApiError("HTTP 409 for request request-test", status=409))
            api.snapshot = lambda _: AttemptSnapshot("succeeded", "closed")
            with self.assertRaises(ApiError) as caught:
                ExecutionLoop(api, store, lambda _: self.fail("analysis reran"), lambda *_: None).step()
            self.assertEqual(store.load(), terminal)
            for fact in (ACTIVE.execution_attempt_ref, "snapshot is succeeded", "request-test", "command remains retained"):
                self.assertIn(fact, str(caught.exception))

    def test_second_writer_cannot_take_the_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "journal"
            with AttemptStore(root):
                with self.assertRaises(BlockingIOError):
                    AttemptStore(root)

    def test_only_an_exact_api_receipt_retires_retained_result_or_failure(self):
        for terminal, response_schema in (
            (complete_command(ACTIVE, REPORT), "nmr.provider.execution_attempt_complete_response.v1"),
            (fail_command(ACTIVE, "test_failure", "The worker could not finish."), "nmr.provider.execution_attempt_fail_response.v1"),
        ):
            with self.subTest(operation=terminal.operation), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as store:
                    store.save(terminal)
                    command = json.loads(terminal.body)
                    receipt = {"schema_id": response_schema,
                               "execution_attempt_ref": ACTIVE.execution_attempt_ref}
                    if terminal.operation == "complete":
                        raw = b64decode(command["canonical_result_base64"])
                        receipt.update(result_schema_id=REPORT["schema_id"], result_byte_length=len(raw),
                                       result_fingerprint="sha256:" + sha256(raw).hexdigest())
                        fact = "result_fingerprint"
                    else:
                        receipt.update(failure_code=command["failure_code"], failure_message=command["failure_message"])
                        fact = "failure_message"
                    class Transport:
                        provider_ref = START.provider_ref
                        def request(self, operation, **kwargs):
                            return self.reply
                    transport = Transport()
                    loop = ExecutionLoop(JobApi(transport), store, lambda _: self.fail("Analysis was rerun"), lambda *_: None)
                    for reply in (b"private invalid JSON", json.dumps(receipt | {fact: "different"}).encode()):
                        transport.reply = reply
                        with self.assertRaises(ApiError) as caught:
                            loop.step()
                        self.assertEqual(store.load(), terminal)
                        if reply.startswith(b"private"):
                            self.assertIn("Cannot confirm the Provider API request to publish", str(caught.exception))
                            self.assertNotIn("private", str(caught.exception))
                    transport.reply = json.dumps(receipt).encode()
                    loop.step()
                    self.assertIsNone(store.load())
