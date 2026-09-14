"""Current API explanations and malformed Problems must preserve retained work.

Wire examples follow API 1708afc2b1a213b4b7c71d6ac4421284b9983834:
problem_kinds.py, security/problems.py and provider/routes.py. These tests use
existing request and lifecycle consumers, without importing the API server.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.attempt_store import AttemptStore
from secs_inference.provider.execution import ExecutionLoop
from secs_inference.provider.http import HttpResponse, HttpsEndpoint
from secs_inference.provider.job_api import ApiError, ApiUnavailable, AttemptSnapshot, JobApi, fail_command
from secs_inference.provider.operations import Operation
from test_execution import ACTIVE, START


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
