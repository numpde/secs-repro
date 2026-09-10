"""Explain transport failures without exposing exception-owned request data."""

import http.client
import os
import socket
import ssl
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AddressFailure:
    """One attempted resolved destination, without exception-owned text in repr."""

    family: int
    address: tuple
    cause: OSError = field(repr=False)
    cleanup_cause: OSError | None = field(default=None, repr=False)


class ConnectionFailed(OSError):
    """TCP connection attempts failed before TLS or HTTP began."""

    def __init__(self, attempts: tuple[AddressFailure, ...], *, unattempted_count: int = 0):
        super().__init__("TCP connection could not be established")
        self.attempts = attempts
        self.unattempted_count = unattempted_count


def network_failure_evidence(error: BaseException) -> dict:
    """Retain safe machine evidence, including every failed resolved destination."""
    evidence = {"exception_type": type(error).__name__, "errno": getattr(error, "errno", None)}
    if isinstance(error, ConnectionFailed):
        evidence["connection_attempts"] = [
            {"family": _family_name(item.family), "address": item.address[0], "port": item.address[1],
             **({"flow_info": item.address[2], "scope_id": item.address[3]} if len(item.address) == 4 else {}),
             **network_failure_evidence(item.cause), "reason": network_failure_reason(item.cause),
             **({"socket_cleanup": network_failure_evidence(item.cleanup_cause)} if item.cleanup_cause is not None else {})}
            for item in error.attempts
        ]
        evidence["unattempted_address_count"] = error.unattempted_count
    return evidence


def _family_name(family: int) -> str:
    return "IPv4" if family == socket.AF_INET else "IPv6" if family == socket.AF_INET6 else f"address family {family}"


def network_failure_reason(error: BaseException) -> str:
    """Describe known transport evidence, leaving phase and delivery to callers.

    Exception strings can contain URLs, credentials or remote response bytes.
    OS error descriptions and known exception types need none of those values.
    """
    if isinstance(error, ConnectionFailed):
        # Repeated DNS answers should not drown the public message or its
        # delivery caveat. The private evidence keeps every address in order.
        causes = {}
        for item in error.attempts:
            reason = f"{_family_name(item.family)}: {network_failure_reason(item.cause)}"
            causes[reason] = causes.get(reason, 0) + 1
        summary = "; ".join(f"{reason} ({count} address(es))" for reason, count in list(causes.items())[:4])
        if len(causes) > 4:
            summary += f"; {len(causes) - 4} further cause group(s) are recorded in operator diagnostics"
        if any(item.cleanup_cause is not None for item in error.attempts):
            return (f"connection attempts failed: {summary}; socket cleanup also failed; "
                    f"{error.unattempted_count} further address(es) were not attempted")
        return f"all {len(error.attempts)} resolved addresses failed: {summary}"
    if isinstance(error, socket.gaierror):
        return "the service's network address could not be resolved"
    if isinstance(error, ssl.SSLCertVerificationError):
        return "the service's TLS certificate could not be verified"
    if isinstance(error, ssl.SSLError):
        return "the encrypted connection failed"
    if isinstance(error, TimeoutError):
        return "the connection timed out without completing this step"
    if isinstance(error, http.client.RemoteDisconnected):
        return "the service closed the connection without a reply"
    if isinstance(error, http.client.IncompleteRead):
        return "the reply ended before all its declared bytes arrived"
    if isinstance(error, EOFError):
        return "the reply ended before completion"
    if isinstance(error, (http.client.BadStatusLine, http.client.LineTooLong)):
        return "the service returned an unreadable HTTP response"
    if isinstance(error, http.client.HTTPException):
        return "the HTTP exchange could not be completed"
    if isinstance(error, ConnectionRefusedError):
        return "the connection was refused"
    if isinstance(error, ConnectionResetError):
        return "the connection was reset"
    if isinstance(error, ConnectionAbortedError):
        return "the connection was aborted"
    if isinstance(error, BrokenPipeError):
        return "the connection closed while data was being sent"
    if isinstance(error, OSError):
        return (os.strerror(error.errno) if error.errno else
                "an operating-system error interrupted the connection; no specific cause was available")
    return "the connection failed; no specific cause was available"
