"""Own fresh provider authentication for every send, including command replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_bytes
from time import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from secs_inference.provider.hello import (
    HelloAccepted,
    HelloReceiptRejected,
    PreparedHello,
    is_fixed_hello_problem,
    parse_hello_receipt,
)
from secs_inference.provider.http import (
    HttpOutcome,
    HttpResponse,
    HttpsEndpoint,
    send_hello_request,
    send_provider_request,
    TlsRejected,
    ResponseRejected,
    RequestUnavailable,
    RequestDelivery,
)
from secs_inference.provider.signing import sign_request
from secs_inference.provider.operations import Operation
from secs_inference.provider.job_api import ApiError, ApiUnavailable
from secs_inference.provider.response_json import response_object
from secs_inference.provider.network_errors import network_failure_reason, network_failure_evidence


@dataclass(frozen=True, slots=True)
class HelloUnavailable:
    """One send produced no identity-bound hello acceptance receipt."""

    evidence: HttpOutcome | HelloReceiptRejected = field(repr=False)


@dataclass(frozen=True, slots=True)
class HelloCorrectionRequired:
    """The API rejected fixed hello facts that require a new process input."""

    response: HttpResponse = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderApi:
    """Authenticate hello and execution requests with one provider credential."""

    endpoint: HttpsEndpoint
    provider_ref: str
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def request(self, operation: Operation, *, path: str | None = None, query: str = "", body: bytes | None = None) -> bytes:
        """Send once with fresh authentication; execution policy owns all retries."""
        signed = sign_request(
            private_key=self.private_key, credential_ref=self.credential_ref,
            method=operation.method, authority=self.endpoint.authority,
            path=operation.path if path is None else path, query=query, body=body,
            created=int(time()), nonce=token_bytes(16),
        )
        outcome = send_provider_request(endpoint=self.endpoint, request=signed, operation=operation)
        operation_name = f"Provider API request to {operation.action}"
        if isinstance(outcome, TlsRejected):
            raise ApiError(f"Cannot finish {operation_name}: {network_failure_reason(outcome.cause)}; the request was not sent",
                           diagnostic={"operation": operation.action, **network_failure_evidence(outcome.cause)})
        if isinstance(outcome, ResponseRejected):
            raise ApiError(f"Cannot confirm the outcome of the {operation_name}: HTTP {outcome.status} response was rejected because {outcome.reason.explanation}",
                           diagnostic={"operation": operation.action, "status": outcome.status, "response_rejection": outcome.reason.value})
        if isinstance(outcome, RequestUnavailable):
            delivery = {
                RequestDelivery.NOT_SENT: "the request was not sent",
                RequestDelivery.POSSIBLE: "the request may have reached the API; its outcome is unknown",
                RequestDelivery.RESPONSE_RECEIVED: "a reply arrived, but the API outcome could not be confirmed",
            }[outcome.delivery]
            reason = (f"HTTP {outcome.status} did not yield an admitted API response" if outcome.status is not None
                      else network_failure_reason(outcome.cause) if outcome.cause is not None
                      else "no complete response was received")
            raise ApiUnavailable(f"Cannot confirm the outcome of the {operation_name}: {reason}; {delivery}", diagnostic={
                "operation": operation.action, "delivery": outcome.delivery.value,
                "status": outcome.status, **(network_failure_evidence(outcome.cause) if outcome.cause is not None else {}),
            })
        if outcome.status == 200:
            return outcome.body
        request = " without a request ID" if outcome.request_id is None else f" for request {outcome.request_id}"
        # Only these problem meanings authorize retirement or reconciliation.
        # A status line alone, even over TLS, is not their application receipt.
        if outcome.status in {404, 409}:
            meaning = "not-found" if outcome.status == 404 else "operation-conflict"
            try:
                problem = response_object(outcome.body)
            except (ValueError, UnicodeError, RecursionError):
                problem = {}
            if problem.get("type") != "urn:nmr-api:problem:" + meaning or problem.get("status") != outcome.status:
                raise ApiError(f"Cannot reconcile {operation_name} HTTP {outcome.status}{request}: its problem meaning is unreadable")
        error_type = ApiUnavailable if outcome.status in {408, 503} else ApiError
        raise error_type(f"{operation_name} returned HTTP {outcome.status}{request}", status=outcome.status)

    def publish_hello(
        self,
        prepared: PreparedHello,
    ) -> HelloAccepted | HelloCorrectionRequired | HelloUnavailable:
        """Sign, send, and validate one complete provider hello snapshot."""

        signed = sign_request(
            private_key=self.private_key,
            credential_ref=self.credential_ref,
            method=prepared.method,
            authority=self.endpoint.authority,
            path=prepared.path,
            query=prepared.query,
            body=prepared.body,
            created=int(time()),
            nonce=token_bytes(16),
        )
        outcome = send_hello_request(endpoint=self.endpoint, request=signed)
        if type(outcome) is not HttpResponse:
            return HelloUnavailable(outcome)
        if outcome.status == 200:
            receipt = parse_hello_receipt(
                outcome.body,
                expected_provider_ref=self.provider_ref,
            )
            if type(receipt) is HelloAccepted:
                return receipt
            return HelloUnavailable(receipt)
        if is_fixed_hello_problem(
            outcome.body,
            status=outcome.status,
        ):
            return HelloCorrectionRequired(outcome)
        return HelloUnavailable(outcome)
