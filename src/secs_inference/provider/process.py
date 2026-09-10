"""Publish the provider's complete hello snapshot until process shutdown."""

from __future__ import annotations

import logging
import json
from threading import Event

from secs_inference.provider.api import (
    HelloCorrectionRequired,
    HelloUnavailable,
    ProviderApi,
)
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.hello import (
    HelloAccepted,
    HelloReceiptRejected,
    PreparedHello,
)
from secs_inference.provider.http import (
    HttpOutcome,
    HttpResponse,
    RequestUnavailable,
    RequestDelivery,
    ResponseRejected,
    TlsRejected,
)
from secs_inference.provider.network_errors import network_failure_reason, network_failure_evidence
from secs_inference.provider.job_api import ApiError
from secs_inference.provider.problem import describe_problem


_LOG = logging.getLogger(__name__)
_MAX_RETRY_SECONDS = 300.0


def publish_hello_until_stopped(
    *,
    api: ProviderApi,
    prepared: PreparedHello,
    policy: HelloPolicy,
    stop: Event,
) -> None:
    """Publish immediately, then refresh or retry until shutdown is requested."""

    outage_active = False
    retry_seconds = min(policy.retry_initial_seconds, _MAX_RETRY_SECONDS)
    while not stop.is_set():
        outcome = api.publish_hello(prepared)
        if type(outcome) is HelloAccepted:
            if outage_active:
                _LOG.info("Provider hello recovered")
            else:
                _LOG.info("Provider hello published")
            outage_active = False
            retry_seconds = min(policy.retry_initial_seconds, _MAX_RETRY_SECONDS)
            wait_seconds = policy.publication_interval_seconds
        elif type(outcome) is HelloCorrectionRequired:
            explanation, diagnostic = describe_problem(outcome.response)
            raise ApiError(
                "The Provider API rejected the hello request. Correct the "
                "provider configuration or code before restarting: "
                + explanation,
                diagnostic={"operation": "publish hello", **diagnostic},
            )
        else:
            # Backoff bounds log volume. Each request can reveal a different
            # failure or request ID even while registration remains unavailable.
            evidence = outcome.evidence
            cause = evidence.cause if isinstance(evidence, (TlsRejected, RequestUnavailable)) else None
            _LOG.warning(
                "Provider hello is unavailable; retrying in %g seconds: %s%s",
                retry_seconds, _evidence_message(evidence),
                " | " + json.dumps(network_failure_evidence(cause)) if cause is not None else "",
            )
            outage_active = True
            wait_seconds = retry_seconds
            retry_seconds = min(retry_seconds * 2.0, _MAX_RETRY_SECONDS)
        stop.wait(wait_seconds)


def _evidence_message(evidence: HttpOutcome | HelloReceiptRejected) -> str:
    """Describe failure evidence without logging remote response bodies."""

    if type(evidence) is HttpResponse:
        return describe_problem(evidence)[0]
    if type(evidence) is HelloReceiptRejected:
        return f"HTTP 200 did not confirm hello acceptance: {evidence.reason.explanation}"
    if type(evidence) is ResponseRejected:
        request = f"; response request ID {evidence.request_id}" if evidence.request_id is not None else ""
        return (
            f"HTTP response was rejected: {evidence.reason.explanation}; "
            f"status={evidence.status}{request}"
        )
    if type(evidence) is TlsRejected:
        return f"{network_failure_reason(evidence.cause)}; the request was not sent"
    if type(evidence) is RequestUnavailable:
        delivery = {
            RequestDelivery.NOT_SENT: "the hello request was not sent",
            RequestDelivery.POSSIBLE: "the hello request may have reached the API; acceptance is unknown",
            RequestDelivery.RESPONSE_RECEIVED: "a reply arrived, but hello acceptance could not be confirmed",
        }[evidence.delivery]
        if evidence.status is not None:
            delivery = f"HTTP {evidence.status} ended without a complete API response; {delivery}"
        if evidence.request_id is not None:
            delivery += f"; response request ID {evidence.request_id}"
        return delivery if evidence.cause is None else f"{delivery}; {network_failure_reason(evidence.cause)}"
    raise AssertionError("Remote provider evidence has no operator description")
