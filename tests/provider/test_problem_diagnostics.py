"""API explanations survive both job requests and hello's operator surface."""

import json
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import ProviderApi
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.hello import prepare_hello
from secs_inference.provider.http import HttpResponse, HttpsEndpoint
from secs_inference.provider.job_api import ApiError, ApiUnavailable
from secs_inference.provider.operations import Operation
from secs_inference.provider.process import publish_hello_until_stopped
from test_provider_process import StopAfterWaits


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
