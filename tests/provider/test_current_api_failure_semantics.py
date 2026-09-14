"""Current API explanations and malformed Problems must preserve retained work.

Wire examples follow API 1708afc2b1a213b4b7c71d6ac4421284b9983834:
problem_kinds.py, security/problems.py and provider/routes.py. These tests use
existing request and lifecycle consumers, without importing the API server.
"""

import json
from base64 import b64encode
from hashlib import sha256
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.attempt_store import AttemptStore, JournalError
from secs_inference.provider.attempt_state import TerminalHold
from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.http import HttpResponse, HttpsEndpoint, RequestDelivery, RequestUnavailable
from secs_inference.provider.job_api import ApiError, ApiUnavailable, AttemptSnapshot, JobApi, fail_command, complete_command
from secs_inference.provider.operations import Operation
from test_execution import ACTIVE, START, REPORT


REQUEST_ID = "request:failure-contract-test"
INSTANCE = "urn:nmr-api:request:request%3Afailure-contract-test"


def response(status, kind, title, code, detail, **changes):
    body = dict(type="urn:nmr-api:problem:" + kind, title=title, status=status,
                request_id=REQUEST_ID, instance=INSTANCE, code=code, detail=detail)
    body.update(changes)
    return HttpResponse(status, REQUEST_ID, json.dumps(body).encode())


class CurrentApiFailureSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.transport = ProviderApi(HttpsEndpoint("https://api.test", "web", 1, 1),
                                    START.provider_ref, "credential:test", Ed25519PrivateKey.generate())

    def test_same_status_availability_and_unconfirmed_change_explanations_reach_caller(self):
        recovery = (
            "Retry saving the computation's failure report by resending the unchanged "
            "fail request body, including execution_attempt_ref, to the same endpoint. "
            "An identical retry does not change an outcome already recorded. Do not "
            "rerun the computation or report a different terminal outcome to recover this request."
        )
        cases = (
            ("service-unavailable", "Service unavailable", "authentication_replay_ledger_unavailable",
             "The server could not confirm recording this request's one-use authentication value (nonce). "
             "This attempt did not run the requested operation. The nonce may nevertheless "
             "have been recorded, so do not resend the same signed request. "
             "Retry the operation through the app with a fresh signature and nonce. "
             "If this continues, ask the server administrator to investigate authentication replay storage."),
            ("mutation-outcome-unconfirmed", "Change outcome unconfirmed", "service_unavailable",
             "The API could not confirm whether this request's change was saved. "
             + recovery + " The app must sign the retry with a fresh nonce."),
        )
        for kind, title, code, detail in cases:
            with self.subTest(kind=kind), patch("secs_inference.provider.api.send_provider_request",
                                               return_value=response(503, kind, title, code, detail)):
                with self.assertRaises(ApiUnavailable) as caught:
                    self.transport.request(Operation.FAIL, body=fail_command(ACTIVE, "test_failure", "Analysis failed.").body)
                # This exception is the existing operator/caller explanation surface.
                self.assertIn(detail, str(caught.exception))
                self.assertEqual(caught.exception.diagnostic["problem_type"], "urn:nmr-api:problem:" + kind)
                self.assertEqual(caught.exception.diagnostic["code"], code)

    def test_terminal_conflict_preserves_report_and_blocks_resend_after_restart(self):
        for operation, code, state in (
            ("complete", "execution_attempt_outcome_expired", "expired"),
            ("fail", "execution_attempt_outcome_expired", "expired"),
            ("complete", "execution_attempt_completion_after_failure", "failed"),
            ("fail", "execution_attempt_failure_after_success", "succeeded"),
            ("complete", "execution_attempt_completion_replay_mismatch", "succeeded"),
            ("fail", "execution_attempt_failure_replay_mismatch", "failed"),
        ):
            with self.subTest(operation=operation, code=code), TemporaryDirectory() as directory:
                terminal = (complete_command(ACTIVE, REPORT) if operation == "complete"
                            else fail_command(ACTIVE, "test_failure", "Analysis failed."))
                api = JobApi(self.transport)
                analyse = Mock(side_effect=AssertionError("Pending terminal work must not rerun"))
                conflict = response(409, "operation-conflict", "Operation conflict", code,
                                    "This terminal request cannot be applied; reconcile the original command.")
                with AttemptStore(Path(directory) / "journal") as journal:
                    journal.save(terminal)
                    with patch("secs_inference.provider.api.send_provider_request", side_effect=[
                            RequestUnavailable(RequestDelivery.POSSIBLE), conflict]), patch.object(
                            api, "snapshot", return_value=AttemptSnapshot(state, "open")) as snapshot:
                        loop = ExecutionLoop(api, journal, analyse, journal.diagnose)
                        with self.assertRaises(ApiUnavailable):
                            loop.step()
                        try:
                            loop.step()
                        except ApiError:
                            pass
                    snapshot.assert_not_called()
                    held = journal.load()
                    self.assertIsNotNone(held, "Refusal cannot discard the exact pending report")
                    self.assertEqual(held.body, terminal.body)
                    self.assertEqual(held.active, terminal.active)
                    self.assertEqual(held.operation, terminal.operation)
                    self.assertEqual(held.hold.code, code)
                    self.assertEqual(held.hold.request_id, REQUEST_ID)
                    self.assertIn(held.hold.action, {"do_not_resend", "reconcile_original"})
                    self.assertTrue(held.hold.description)
                    self.assertIn("reconcile the original command", held.hold.detail)
                    self.assertEqual(json.loads((journal.directory / "attempt.json").read_bytes())["stage"], "terminal_held")
                with AttemptStore(Path(directory) / "journal") as journal, patch.object(
                        api, "publish", side_effect=AssertionError("Restart must not resend a held command")) as publish:
                    try:
                        ExecutionLoop(api, journal, analyse, journal.diagnose).step()
                    except ApiError as error:
                        self.assertIn("retained", str(error))
                        evidence = error.diagnostic["terminal_hold"]
                        self.assertEqual(evidence["execution_attempt_ref"], ACTIVE.execution_attempt_ref)
                        self.assertEqual(evidence["operation"], operation)
                        self.assertEqual(evidence["command_fingerprint"], "sha256:" + sha256(terminal.body).hexdigest())
                        self.assertTrue(evidence["command_retained"])
                        self.assertEqual(evidence["delivery"], "unconfirmed")
                        self.assertEqual(evidence["automatic_resends"], "stopped_including_restart")
                        self.assertEqual(evidence["next_actor"], "provider_operator")
                        self.assertIn("reconcile", evidence["next_action"])
                        self.assertNotIn("body", evidence)
                    publish.assert_not_called()
                    self.assertEqual(journal.load().body, terminal.body)
                analyse.assert_not_called()

    def test_malformed_missing_or_mismatched_correlation_cannot_trigger_terminal_reconciliation(self):
        terminal = fail_command(ACTIVE, "test_failure", "Analysis failed.")
        for status, kind, title, code in (
            (404, "not-found", "Resource not found", "resource_not_found"),
            (409, "operation-conflict", "Operation conflict", "operation_conflict"),
        ):
            valid = json.loads(response(status, kind, title, code, "The request could not be applied.").body)
            for label, body in (
                ("identity-only", {"type": valid["type"], "status": status}),
                ("mismatched-request-id", valid | {"request_id": "request:another-request"}),
                ("mismatched-instance", valid | {"instance": "urn:nmr-api:request:another-request"}),
            ):
                with self.subTest(status=status, malformed=label), TemporaryDirectory() as directory:
                    with AttemptStore(Path(directory) / "journal") as journal:
                        journal.save(terminal)
                        api = JobApi(self.transport)
                        snapshot = Mock(return_value=AttemptSnapshot("expired", "open"))
                        analyse = Mock(side_effect=AssertionError("Retained failure must not rerun analysis"))
                        with patch("secs_inference.provider.api.send_provider_request", return_value=HttpResponse(
                                status, REQUEST_ID, json.dumps(body).encode())), patch.object(JobApi, "snapshot", snapshot):
                            try:
                                ExecutionLoop(api, journal, analyse, journal.diagnose).step()
                            except ApiError:
                                pass
                        snapshot.assert_not_called()
                        self.assertEqual(journal.load(), terminal)
                        analyse.assert_not_called()

    def test_malformed_hold_stops_before_network(self):
        for changes in ({"action": "retry"}, {"request_id": "x" * 129}, {"description": "bad\nline"}, {"detail": "x" * 1025}, {"detail": "bad\u202etext"},
                        {"observed_state": []}, {"action": "reconcile_state", "observed_state": None},
                        {"description": "\ud800"}, {"code": None}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    journal.save(fail_command(ACTIVE, "test_failure", "Analysis failed."))
                    path = journal.directory / "attempt.json"
                    document = json.loads(path.read_bytes())
                    document.update(stage="terminal_held", hold={"action": "do_not_resend", "code": "execution_attempt_outcome_expired",
                                    "description": "Keep the original report.", "detail": "The Attempt expired.", "request_id": REQUEST_ID,
                                    "observed_state": None} | changes)
                    path.write_text(json.dumps(document))
                    api = Mock(provider_ref=START.provider_ref)
                    with self.assertRaises(JournalError):
                        ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                    api.publish.assert_not_called()
                    api.snapshot.assert_not_called()

    def test_failed_hold_write_stops_without_another_network_operation(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            terminal = fail_command(ACTIVE, "test_failure", "Analysis failed.")
            journal.save(terminal)
            api = JobApi(self.transport)
            conflict = response(409, "operation-conflict", "Operation conflict", "execution_attempt_outcome_expired", "The Attempt expired.")
            with patch("secs_inference.provider.api.send_provider_request", return_value=conflict) as send, patch(
                    "secs_inference.provider.attempt_store.os.fsync", side_effect=OSError("disk failed")):
                loop = ExecutionLoop(api, journal, Mock(), journal.diagnose)
                with self.assertRaises(JournalError):
                    loop.step()
                with self.assertRaises(JournalError):
                    loop.step()
                self.assertEqual(send.call_count, 1)
            self.assertEqual(json.loads((journal.directory / "attempt.json").read_bytes())["stage"], "terminal")

    def test_generic_refusal_reads_first_then_retains_missing_attempt_without_resending(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            terminal = fail_command(ACTIVE, "test_failure", "Analysis failed.")
            journal.save(terminal)
            api = JobApi(self.transport)
            conflict = response(409, "operation-conflict", "Operation conflict", "operation_conflict", "The operation conflicts with current state.")
            with patch("secs_inference.provider.api.send_provider_request", return_value=conflict) as send, patch.object(
                    api, "snapshot", side_effect=ApiError("Not visible", status=404)) as snapshot:
                loop = ExecutionLoop(api, journal, Mock(), journal.diagnose)
                with self.assertRaises(ApiError):
                    loop.step()
                with self.assertRaises(ApiError):
                    loop.step()
                snapshot.assert_called_once_with(ACTIVE)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(journal.load().body, terminal.body)
                self.assertEqual(journal.load().hold.observed_state, "not_visible")

    def test_downgraded_or_missing_hold_cannot_authorize_resend(self):
        for stage, include_hold in (("terminal", True), ("terminal_held", False)):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    journal.save(fail_command(ACTIVE, "test_failure", "Analysis failed."))
                    path = journal.directory / "attempt.json"
                    document = json.loads(path.read_bytes())
                    document["stage"] = stage
                    if include_hold:
                        document["hold"] = {"action": "do_not_resend"}
                    path.write_text(json.dumps(document))
                    api = Mock(provider_ref=START.provider_ref)
                    with self.assertRaises(JournalError):
                        ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                    api.publish.assert_not_called()

    def test_held_command_requires_canonical_bound_body_before_claiming_retention(self):
        original = fail_command(ACTIVE, "test_failure", "Analysis failed.")
        command = json.loads(original.body)
        for body in (b"not JSON", json.dumps(command).encode(), canonical_json_bytes(command | {
                "execution_attempt_ref": "execution_attempt:sha256:" + "c" * 64}),
                canonical_json_bytes(command | {"schema_id": "nmr.provider.execution_attempt_complete_request.v1"})):
            with self.subTest(body=body), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    hold = TerminalHold("do_not_resend", "execution_attempt_outcome_expired", "Do not resend.",
                                        "The Attempt expired.", REQUEST_ID)
                    journal.save(replace(original, hold=hold))
                    path = journal.directory / "attempt.json"
                    document = json.loads(path.read_bytes())
                    document["body_base64"] = b64encode(body).decode("ascii")
                    path.write_text(json.dumps(document))
                    api = Mock(provider_ref=START.provider_ref)
                    with self.assertRaises(JournalError):
                        ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                    api.publish.assert_not_called()

    def test_generic_conflict_with_live_snapshot_requires_reconciliation_across_restart(self):
        with TemporaryDirectory() as directory:
            terminal = complete_command(ACTIVE, REPORT)
            api = JobApi(self.transport)
            conflict = response(409, "operation-conflict", "Operation conflict", "operation_conflict",
                                "The operation conflicts with current state.")
            with AttemptStore(Path(directory) / "journal") as journal:
                journal.save(terminal)
                with patch("secs_inference.provider.api.send_provider_request", return_value=conflict), patch.object(
                        api, "snapshot", return_value=AttemptSnapshot("in_progress", "open")) as snapshot:
                    with self.assertRaises(ApiError):
                        ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                    snapshot.assert_called_once_with(ACTIVE)
            with AttemptStore(Path(directory) / "journal") as journal, patch.object(
                    api, "publish", side_effect=AssertionError("An unresolved conflict does not permit another send")) as publish:
                with self.assertRaises(ApiError) as caught:
                    ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                publish.assert_not_called()
                held = journal.load()
                self.assertEqual(held.body, terminal.body)
                self.assertEqual(held.hold.observed_state, "in_progress")
                self.assertEqual(held.hold.action, "reconcile_state")
                self.assertEqual(caught.exception.diagnostic["terminal_hold"]["automatic_resends"], "stopped_including_restart")

    def test_conflict_read_outage_retries_only_reconciliation_across_restart(self):
        with TemporaryDirectory() as directory:
            terminal = complete_command(ACTIVE, REPORT)
            api = JobApi(self.transport)
            conflict = response(409, "operation-conflict", "Operation conflict", "operation_conflict",
                                "The operation conflicts with current state.")
            for phase in ("initial", "restart"):
                with AttemptStore(Path(directory) / "journal") as journal:
                    if phase == "initial":
                        journal.save(terminal)
                    def unavailable_read(active):
                        self.assertEqual(json.loads((journal.directory / "attempt.json").read_bytes())["stage"], "terminal_reconciling")
                        raise ApiUnavailable("Read temporarily unavailable")
                    with patch("secs_inference.provider.api.send_provider_request", return_value=conflict) as send, patch.object(
                            api, "snapshot", side_effect=unavailable_read) as snapshot:
                        with self.assertRaises(ApiUnavailable) as caught:
                            ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                        self.assertIn("read", str(caught.exception))
                        self.assertIn("retained", str(caught.exception))
                        self.assertEqual(send.call_count, 1 if phase == "initial" else 0)
                        snapshot.assert_called_once_with(ACTIVE)
                        self.assertEqual(journal.load().body, terminal.body)
            with AttemptStore(Path(directory) / "journal") as journal, patch.object(api, "publish") as publish, patch.object(
                    api, "snapshot", return_value=AttemptSnapshot("in_progress", "open")) as snapshot:
                with self.assertRaises(ApiError):
                    ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                publish.assert_not_called()
                snapshot.assert_called_once_with(ACTIVE)
                self.assertEqual(journal.load().hold.observed_state, "in_progress")
                self.assertEqual(json.loads((journal.directory / "attempt.json").read_bytes())["stage"], "terminal_held")

    def test_pending_reconciliation_write_failure_prevents_first_read(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            journal.save(complete_command(ACTIVE, REPORT))
            api = JobApi(self.transport)
            conflict = response(409, "operation-conflict", "Operation conflict", "operation_conflict", "Conflict.")
            with patch("secs_inference.provider.api.send_provider_request", return_value=conflict) as send, patch.object(
                    api, "snapshot", return_value=AttemptSnapshot("expired", "open")) as snapshot, patch(
                    "secs_inference.provider.attempt_store.os.fsync", side_effect=OSError("disk failed")):
                with self.assertRaises(JournalError):
                    ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                self.assertEqual(send.call_count, 1)
                snapshot.assert_not_called()

    def test_reconciling_stage_rejects_observation_or_different_action(self):
        for changes in ({"action": "do_not_resend"}, {"observed_state": "expired"}):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    hold = TerminalHold("reconcile_state", "operation_conflict", "Reconcile state.", "Conflict.", REQUEST_ID)
                    journal.save(replace(complete_command(ACTIVE, REPORT), hold=hold))
                    path = journal.directory / "attempt.json"
                    document = json.loads(path.read_bytes())
                    document["hold"].update(changes)
                    path.write_text(json.dumps(document))
                    with self.assertRaises(JournalError):
                        journal.load()

    def test_malformed_read_leaves_pending_reconciliation_without_publication(self):
        with TemporaryDirectory() as directory, AttemptStore(Path(directory) / "journal") as journal:
            hold = TerminalHold("reconcile_state", "operation_conflict", "Reconcile state.", "Conflict.", REQUEST_ID)
            terminal = replace(complete_command(ACTIVE, REPORT), hold=hold)
            journal.save(terminal)
            api = JobApi(self.transport)
            with patch.object(api, "publish") as publish, patch.object(api, "snapshot", side_effect=ApiError(
                    "Unverified read", diagnostic={"status": 404, "problem_verified": False})):
                with self.assertRaises(ApiError) as caught:
                    ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                self.assertNotIsInstance(caught.exception, ApiUnavailable)
                self.assertEqual(caught.exception.diagnostic["terminal_reconciliation"]["automatic_reads"], "stopped")
                self.assertEqual(journal.load(), terminal)
                publish.assert_not_called()

    def test_api_valid_unicode_spacing_survives_terminal_hold_restart(self):
        for detail in ("The Attempt\u00a0expired.", "The Attempt\u2003expired."):
            with self.subTest(detail=detail), TemporaryDirectory() as directory:
                api = JobApi(self.transport)
                terminal = complete_command(ACTIVE, REPORT)
                with AttemptStore(Path(directory) / "journal") as journal:
                    journal.save(terminal)
                    refusal = response(409, "operation-conflict", "Operation conflict", "execution_attempt_outcome_expired", detail)
                    with patch("secs_inference.provider.api.send_provider_request", return_value=refusal):
                        with self.assertRaises(ApiError):
                            ExecutionLoop(api, journal, Mock(), journal.diagnose).step()
                with AttemptStore(Path(directory) / "journal") as journal:
                    self.assertEqual(journal.load().hold.detail, detail)
                    self.assertEqual(journal.load().body, terminal.body)

    def test_stored_api_evidence_rejects_forbidden_spaces_controls_and_request_ids(self):
        for name, value in (("detail", " leading"), ("detail", "trailing "), ("detail", "\u00a0leading"),
                            ("detail", "trailing\u2003"), ("detail", "hidden\u0085control"),
                            ("detail", "hidden\u202econtrol"), ("request_id", "request with space"),
                            ("request_id", "request:\u00e9")):
            with self.subTest(name=name, value=value), TemporaryDirectory() as directory:
                with AttemptStore(Path(directory) / "journal") as journal:
                    hold = TerminalHold("do_not_resend", "execution_attempt_outcome_expired", "Do not resend.", "Expired.", REQUEST_ID)
                    journal.save(replace(complete_command(ACTIVE, REPORT), hold=replace(hold, **{name: value})))
                    with self.assertRaises(JournalError):
                        journal.load()
