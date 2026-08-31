"""Publish the provider's complete hello snapshot until process shutdown."""

from __future__ import annotations

import logging
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
    ResponseRejected,
    TlsRejected,
)


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
            raise RuntimeError(
                "The Provider API rejected the hello request. Correct the "
                "provider configuration or code before restarting: "
                f"{_evidence_message(outcome.response)}"
            )
        else:
            if not outage_active:
                # Remote deployment and authorization can recover without a
                # local restart, so rejection evidence follows retry policy too.
                # Log the outage once; retries change cadence but add no new
                # operator evidence until publication recovers.
                _LOG.warning(
                    "Provider hello is unavailable; retrying: %s",
                    _evidence_message(outcome.evidence),
                )
            outage_active = True
            wait_seconds = retry_seconds
            retry_seconds = min(retry_seconds * 2.0, _MAX_RETRY_SECONDS)
        stop.wait(wait_seconds)


def _evidence_message(evidence: HttpOutcome | HelloReceiptRejected) -> str:
    """Describe failure evidence without logging remote response bodies."""

    if type(evidence) is HttpResponse:
        request = (
            " without a request ID"
            if evidence.request_id is None
            else f" for request {evidence.request_id}"
        )
        return f"HTTP {evidence.status}{request}"
    if type(evidence) is HelloReceiptRejected:
        return f"HTTP 200 receipt was rejected: {evidence.reason.value}"
    if type(evidence) is ResponseRejected:
        return (
            f"HTTP response was rejected: {evidence.reason.value}; "
            f"status={evidence.status}"
        )
    if type(evidence) is TlsRejected:
        return "TLS verification failed before the request was sent"
    if type(evidence) is RequestUnavailable:
        if evidence.status is not None:
            return f"HTTP {evidence.status} ended without a complete API response"
        delivery = evidence.delivery.value.replace("_", " ")
        if evidence.cause is None:
            return f"request delivery was {delivery}"
        return f"request delivery was {delivery}; {type(evidence.cause).__name__}"
    raise AssertionError("Remote provider evidence has no operator description")
