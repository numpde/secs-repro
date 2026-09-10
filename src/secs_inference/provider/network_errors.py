"""Explain transport failures without exposing exception-owned request data."""

import http.client
import os
import socket
import ssl


def network_failure_reason(error: BaseException) -> str:
    """Describe known transport evidence, leaving phase and delivery to callers.

    Exception strings can contain URLs, credentials or remote response bytes.
    OS error descriptions and known exception types need none of those values.
    """
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
