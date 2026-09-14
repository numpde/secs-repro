"""API explanations survive both job requests and hello's operator surface."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.hello import prepare_hello
from secs_inference.provider.http import (
    HttpResponse, HttpsEndpoint, RequestDelivery, RequestUnavailable, ResponseRejected, ResponseRejection,
)
from secs_inference.provider.job_api import ApiError, ApiUnavailable, JobApi
from secs_inference.provider.operations import Operation
from secs_inference.provider.process import publish_hello_until_stopped
from test_provider_process import StopAfterWaits
from test_execution import ACTIVE, START, REPORT
from secs_inference.provider.job_api import complete_command
from secs_inference.provider.job_upload import JobUpload


class ProblemDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.api = ProviderApi(HttpsEndpoint("https://api.test", "web", 1, 1),
                               "provider:test", "credential:test", Ed25519PrivateKey.generate())
        self.problem = {"type": "urn:nmr-api:problem:bad-request", "title": "Bad request", "status": 400,
                        "request_id": "request-test", "instance": "/private-path",
                        "code": "provider_request_invalid", "detail": "The request requires an analysis kind."}

    def response(self, problem):
        return HttpResponse(problem["status"], "request-test", json.dumps(problem).encode())

    def request_error(self, response, operation=Operation.JOBS):
        with patch("secs_inference.provider.api.send_provider_request", return_value=response):
            with self.assertRaises(ApiError) as caught:
                self.api.request(operation, path=operation.path.format(
                    job_ref="job:test", upload_ref="upload:test", execution_attempt_ref="execution_attempt:test"))
        return caught.exception

    def hello(self, response):
        with patch("secs_inference.provider.api.send_hello_request", return_value=response):
            publish_hello_until_stopped(api=self.api,
                prepared=prepare_hello(display_name="Provider", description="Description", analysis_offerings=()),
                policy=HelloPolicy("Provider", "Description", 3600, 5), stop=StopAfterWaits(1))

    def test_shared_operation_mapping_matches_existing_route_identity(self):
        from secs_inference.provider.problem import _API_OPERATIONS
        document = json.loads((Path("/workspace/contracts/upstream/nmr_api_v1/openapi/openapi.v1.json")).read_bytes())
        self.assertEqual(set(_API_OPERATIONS), set(Operation))
        for operation, operation_id in _API_OPERATIONS.items():
            with self.subTest(operation=operation):
                self.assertEqual(operation_id, document["paths"][operation.path][operation.method.lower()]["operationId"])

    def test_current_authorization_explanation_reaches_requests_and_hello_logs(self):
        problem = {"type": "urn:nmr-api:problem:authorization-denied", "title": "Authorization denied",
                   "status": 403, "request_id": "request-test", "instance": "urn:nmr-api:request:request-test",
                   "code": "authorization_denied",
                   "detail": "This credential cannot act as a provider. Ask the administrator to review its access."}
        response = self.response(problem)
        for operation in Operation:
            with self.subTest(operation=operation):
                error = self.request_error(response, operation)
                self.assertIn(operation.action, str(error))
                self.assertIn(problem["detail"], str(error))
                self.assertEqual(error.diagnostic["code"], problem["code"])
                self.assertTrue(error.diagnostic["problem_verified"])
        with self.assertLogs("secs_inference.provider.process", level="WARNING") as logs:
            self.hello(response)
        self.assertIn(problem["detail"], " ".join(logs.output))
        self.assertIn("retrying in 5 seconds", " ".join(logs.output))

    def test_unverified_authorization_explanation_keeps_safe_evidence_on_both_surfaces(self):
        problem = {"type": "urn:nmr-api:problem:authorization-denied", "title": "Authorization denied",
                   "status": 403, "request_id": "body-request", "instance": "urn:nmr-api:request:body-request",
                   "code": "authorization_denied", "detail": "Ask the administrator to review provider access."}
        for changed, reason in (({}, "request_id_mismatch"),
                                ({"request_id": "request-test", "instance": "urn:nmr-api:request:wrong"}, "invalid_instance")):
            with self.subTest(reason=reason):
                response = self.response(problem | changed)
                error = self.request_error(response, Operation.FAIL)
                self.assertFalse(error.diagnostic["problem_verified"])
                self.assertEqual(error.diagnostic["problem_rejection"], reason)
                self.assertEqual(error.diagnostic["body_request_id"], (problem | changed)["request_id"])
                self.assertEqual(error.diagnostic["header_request_id"], "request-test")
                with self.assertLogs("secs_inference.provider.process", level="WARNING") as logs:
                    self.hello(response)
                for message in (str(error), " ".join(logs.output)):
                    self.assertIn(problem["detail"], message)
                    self.assertIn("unverified", message)
                    self.assertIn(reason, message)

    def test_public_input_explanation_survives_every_execution_operation_and_fixed_hello(self):
        response = self.response(self.problem)
        for operation in Operation:
            with self.subTest(operation=operation):
                error = self.request_error(response, operation)
                self.assertIn(operation.action, str(error))
                self.assertIn(self.problem["detail"], str(error))
                self.assertEqual(error.diagnostic["code"], "provider_request_invalid")
                self.assertEqual(error.diagnostic["request_id"], "request-test")
                self.assertNotIn("private-path", str(error))
        with self.assertRaises(ApiError) as caught:
            self.hello(response)
        self.assertIn(self.problem["detail"], str(caught.exception))

    def test_service_failure_preserves_correlation_but_not_private_extensions(self):
        problem = self.problem | {"type": "urn:nmr-api:problem:service-unavailable",
                                  "title": "Service unavailable", "status": 503,
                                  "detail": "private-server-trace", "code": "private_code"}
        response = self.response(problem)
        error = self.request_error(response)
        self.assertIsInstance(error, ApiUnavailable)
        self.assertEqual(error.status, 503)
        with self.assertLogs("secs_inference.provider.process", level="WARNING") as logs:
            self.hello(response)
        for message in (str(error), " ".join(logs.output)):
            self.assertIn("Service unavailable", message)
            self.assertIn("request-test", message)
            self.assertIn("no further public explanation", message)
            self.assertNotIn("private", message)
        self.assertNotIn("detail", error.diagnostic)

    def test_unusable_problem_details_do_not_escape_or_change_http_retry_classification(self):
        bodies = [b"private non-JSON", b'{"status":400,"status":400,"detail":"private"}',
                  json.dumps(self.problem | {"status": 503, "detail": "private"}).encode(),
                  json.dumps(self.problem | {"request_id": "private"}).encode()]
        for detail in ("private\nforged log", "private\u202eforged", "private" * 200, {"private": True}):
            bodies.append(json.dumps(self.problem | {"detail": detail}).encode())
        for body in bodies:
            with self.subTest(body=body[:40]):
                error = self.request_error(HttpResponse(400, "request-test", body))
                self.assertIs(type(error), ApiError)
                self.assertEqual(error.status, 400)
                self.assertIn("request-test", str(error))
                self.assertNotIn("private", str(error))
                self.assertNotIn("detail", error.diagnostic)

    def test_unreadable_conflict_is_evidence_not_authority_to_retire_an_attempt(self):
        for status in (404, 409):
            with self.subTest(status=status):
                error = self.request_error(HttpResponse(status, "request-test", b"unreadable"))
                self.assertIsNone(error.status)
                self.assertEqual(error.diagnostic["status"], status)
                self.assertEqual(error.diagnostic["request_id"], "request-test")

    def test_incomplete_response_correlation_reaches_execution_and_hello_logs(self):
        for response in (
            ResponseRejected(ResponseRejection.INVALID_CONTENT_TYPE, 503, "request-test"),
            RequestUnavailable(RequestDelivery.RESPONSE_RECEIVED, EOFError("private body"), 503, "request-test"),
        ):
            with self.subTest(response=response):
                error = self.request_error(response)
                self.assertEqual(error.diagnostic["request_id"], "request-test")
                with self.assertLogs("secs_inference.provider.process", level="WARNING") as logs:
                    self.hello(response)
                for message in (str(error), " ".join(logs.output)):
                    self.assertIn("request-test", message)
                    self.assertIn("503", message)
                    self.assertNotIn("private body", message)

    def test_rejected_success_receipts_keep_request_correlation(self):
        jobs = JobApi(self.api)
        calls = (jobs.next_job, lambda: jobs.start(START), lambda: jobs.snapshot(ACTIVE),
                 lambda: jobs.specification(ACTIVE), lambda: jobs.uploads(ACTIVE),
                 lambda: jobs.capability(ACTIVE, JobUpload("upload:test", "", 0, None)),
                 lambda: jobs.publish(complete_command(ACTIVE, REPORT)))
        response = HttpResponse(200, "request-test", b"private invalid receipt")
        for call in calls:
            with self.subTest(call=call), patch("secs_inference.provider.api.send_provider_request", return_value=response):
                with self.assertRaises(ApiError) as caught:
                    call()
                self.assertIn("request-test", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertIsNone(caught.exception.status)
                self.assertEqual(caught.exception.diagnostic["request_id"], "request-test")
        with self.assertLogs("secs_inference.provider.process", level="WARNING") as logs:
            self.hello(response)
        self.assertIn("request-test", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    def test_receipt_scope_does_not_reclassify_transport_failures(self):
        for error in (ApiUnavailable("unavailable", status=503), ApiError("missing", status=404),
                      ApiError("conflict", status=409)):
            with self.subTest(status=error.status), patch.object(ProviderApi, "request", side_effect=error):
                with self.assertRaises(ApiError) as caught:
                    JobApi(self.api).capability(ACTIVE, JobUpload("upload:test", "", 0, None))
                self.assertIs(caught.exception, error)

    def test_server_error_retries_reads_and_replayable_commands_not_bearer_issuance(self):
        response = self.response(self.problem | {"status": 500, "type": "urn:nmr-api:problem:internal-error",
                                                 "title": "Internal server error"})
        for operation in Operation:
            with self.subTest(operation=operation):
                error = self.request_error(response, operation)
                self.assertEqual(error.status, 500)
                self.assertEqual(isinstance(error, ApiUnavailable), operation is not Operation.CAPABILITY)
