"""Own fresh authentication and receipt validation for provider hello sends."""

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
)
from secs_inference.provider.signing import sign_request


@dataclass(frozen=True, slots=True)
class HelloUnavailable:
    """One send produced no identity-bound hello acceptance receipt."""

    evidence: HttpOutcome | HelloReceiptRejected = field(repr=False)


@dataclass(frozen=True, slots=True)
class HelloRejected:
    """The API rejected fixed hello facts that require a new process input."""

    response: HttpResponse = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderApi:
    """Authenticated hello transport for one provider credential."""

    endpoint: HttpsEndpoint
    provider_ref: str
    credential_ref: str
    private_key: Ed25519PrivateKey = field(repr=False, compare=False)

    def publish_hello(
        self,
        prepared: PreparedHello,
    ) -> HelloAccepted | HelloRejected | HelloUnavailable:
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
        if (
            type(outcome) is HttpResponse
            and is_fixed_hello_problem(
                outcome.body,
                status=outcome.status,
                transport_request_id=outcome.request_id,
            )
        ):
            return HelloRejected(outcome)
        if type(outcome) is not HttpResponse or outcome.status != 200:
            return HelloUnavailable(outcome)
        receipt = parse_hello_receipt(
            outcome.body,
            expected_provider_ref=self.provider_ref,
        )
        if type(receipt) is HelloAccepted:
            return receipt
        return HelloUnavailable(receipt)
