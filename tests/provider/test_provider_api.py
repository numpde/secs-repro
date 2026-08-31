from base64 import b64decode, b64encode
from unittest.mock import patch
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from secs_inference.provider.api import (
    HelloCorrectionRequired,
    HelloUnavailable,
    ProviderApi,
)
from secs_inference.provider.canonical_json import canonical_json_bytes
from secs_inference.provider.credential import parse_provider_credential
from secs_inference.provider.hello import HelloAccepted, prepare_hello
from secs_inference.provider.http import HttpResponse, HttpsEndpoint
from secs_inference.provider.signing import sign_request


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class ProviderApiTests(unittest.TestCase):
    def test_credential_parser_binds_matching_ed25519_keys(self):
        public_der = PRIVATE_KEY.public_key().public_bytes(
            Encoding.DER,
            PublicFormat.SubjectPublicKeyInfo,
        )
        private_pem = PRIVATE_KEY.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
            NoEncryption(),
        ).decode("ascii")
        raw = canonical_json_bytes(
            {
                "algorithm": "ed25519",
                "credential_ref": "credential:provider:secs",
                "principal_ref": "provider:secs",
                "private_key_pkcs8_pem": private_pem,
                "profile": "dev-local",
                "public_key_spki_der_b64": b64encode(public_der).decode("ascii"),
                "schema_id": "nmr.provider.private_signing_credential.v1",
            }
        ) + b"\n"

        credential = parse_provider_credential(raw)

        self.assertEqual(credential.provider_ref, "provider:secs")
        self.assertEqual(credential.credential_ref, "credential:provider:secs")
        self.assertEqual(
            credential.private_key.private_bytes_raw(),
            PRIVATE_KEY.private_bytes_raw(),
        )

    def test_signature_matches_the_released_body_conformance_vector(self):
        # Pinned from the NMR API v1 signature conformance vectors. These
        # independent bytes catch a coherently wrong local serializer.
        body = (
            b'{"job_ref":"job:conformance","provider_attempt_key":'
            b'"conformance-attempt-1","schema_id":'
            b'"nmr.provider.execution_attempt_start_request.v1"}'
        )
        signed = sign_request(
            private_key=PRIVATE_KEY,
            credential_ref="credential:conformance-provider-ed25519",
            method="POST",
            authority="api.example.test",
            path="/provider/v1/execution-attempts/start",
            query="",
            body=body,
            created=1_784_073_600,
            nonce=bytes(range(32, 48)),
        )

        expected_parameters = (
            '("@method" "@authority" "@path" "@query" "content-type" '
            '"content-digest");created=1784073600;expires=1784073900;'
            'nonce="ICEiIyQlJicoKSorLC0uLw";'
            'keyid="credential:conformance-provider-ed25519";tag="nmr-api-v1"'
        )
        expected_digest = (
            "sha-256=:A6HV8thKfjJREy9SNXTpEStHXiXa0ijUKWpkL68BXus=:"
        )
        expected_base = (
            '"@method": POST\n'
            '"@authority": api.example.test\n'
            '"@path": /provider/v1/execution-attempts/start\n'
            '"@query": ?\n'
            '"content-type": application/json\n'
            f'"content-digest": {expected_digest}\n'
            f'"@signature-params": {expected_parameters}'
        ).encode("ascii")

        self.assertEqual(
            signed.headers["Signature-Input"],
            f"sig1={expected_parameters}",
        )
        self.assertEqual(signed.headers["Content-Digest"], expected_digest)
        signature = b64decode(
            signed.headers["Signature"].removeprefix("sig1=:").removesuffix(":")
        )
        PRIVATE_KEY.public_key().verify(signature, expected_base)

    def test_api_signs_each_send_and_validates_the_receipt(self):
        endpoint = HttpsEndpoint(
            "https://api.example.test",
            "web",
            1,
            1,
        )
        api = ProviderApi(
            endpoint,
            "provider:secs",
            "credential:provider:secs",
            PRIVATE_KEY,
        )
        prepared = prepare_hello(
            display_name="Provider",
            description="Description",
            analysis_offerings=(),
        )
        response = HttpResponse(
            200,
            None,
            b'{"accepted_at":"2026-08-31T12:34:56Z",'
            b'"provider_ref":"provider:secs",'
            b'"schema_id":"nmr.provider.hello_response.v1"}',
        )

        with (
            patch(
                "secs_inference.provider.api.send_hello_request",
                return_value=response,
            ) as send,
            patch(
                "secs_inference.provider.api.time",
                return_value=1_700_000_000,
            ),
            patch(
                "secs_inference.provider.api.token_bytes",
                side_effect=(bytes(range(16)), bytes(range(16, 32))),
            ),
        ):
            first_outcome = api.publish_hello(prepared)
            api.publish_hello(prepared)

        self.assertEqual(
            first_outcome,
            HelloAccepted(),
        )
        first_request = send.call_args_list[0].kwargs["request"]
        second_request = send.call_args_list[1].kwargs["request"]
        self.assertEqual(first_request.authority, "api.example.test")
        self.assertEqual(first_request.body, prepared.body)
        self.assertNotEqual(
            first_request.headers["Signature-Input"],
            second_request.headers["Signature-Input"],
        )

    def test_api_classifies_valid_fixed_request_problems_as_terminal(self):
        endpoint = HttpsEndpoint(
            "https://api.example.test",
            "web",
            1,
            1,
        )
        api = ProviderApi(
            endpoint,
            "provider:secs",
            "credential:provider:secs",
            PRIVATE_KEY,
        )
        prepared = prepare_hello(
            display_name="Provider",
            description="Description",
            analysis_offerings=(),
        )
        profiles = {
            400: (
                "urn:nmr-api:problem:bad-request",
                "Bad request",
                "provider_request_invalid",
            ),
            413: (
                "urn:nmr-api:problem:request-content-too-large",
                "Request content too large",
                "request_content_too_large",
            ),
            414: (
                "urn:nmr-api:problem:uri-too-long",
                "URI too long",
                "request_path_too_large",
            ),
            431: (
                "urn:nmr-api:problem:request-header-fields-too-large",
                "Request header fields too large",
                "request_header_bytes_too_large",
            ),
        }
        for status, (problem_type, title, code) in profiles.items():
            with self.subTest(status=status):
                response = HttpResponse(
                    status,
                    "request-header",
                    canonical_json_bytes(
                        {
                            "code": code,
                            "detail": "The fixed hello request is invalid.",
                            "instance": "/provider/v1/problems/test",
                            "request_id": "request-body",
                            "status": status,
                            "title": title,
                            "type": problem_type,
                        }
                    ),
                )

                with patch(
                    "secs_inference.provider.api.send_hello_request",
                    return_value=response,
                ):
                    outcome = api.publish_hello(prepared)

                self.assertEqual(outcome, HelloCorrectionRequired(response))

    def test_api_keeps_malformed_fixed_request_evidence_unavailable(self):
        endpoint = HttpsEndpoint(
            "https://api.example.test",
            "web",
            1,
            1,
        )
        api = ProviderApi(
            endpoint,
            "provider:secs",
            "credential:provider:secs",
            PRIVATE_KEY,
        )
        prepared = prepare_hello(
            display_name="Provider",
            description="Description",
            analysis_offerings=(),
        )
        invalid_diagnostic = canonical_json_bytes(
            {
                "code": "provider_request_invalid",
                "detail": "The request is invalid.",
                "instance": "/provider/v1/problems/test",
                "request_id": "request-test",
                "status": 413,
                "title": "Request content too large",
                "type": "urn:nmr-api:problem:request-content-too-large",
            }
        )
        responses = (
            HttpResponse(400, "request-test", b"{}"),
            HttpResponse(413, "request-test", invalid_diagnostic),
        )
        for response in responses:
            with self.subTest(status=response.status):
                with patch(
                    "secs_inference.provider.api.send_hello_request",
                    return_value=response,
                ):
                    outcome = api.publish_hello(prepared)

                self.assertEqual(outcome, HelloUnavailable(response))


if __name__ == "__main__":
    unittest.main()
