"""Send the exact signed provider hello over verified, bounded HTTPS."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import http.client
import math
from pathlib import Path
import re
import socket
import ssl
from time import monotonic

from secs_inference.provider.signing import (
    SignedRequest,
    is_canonical_https_authority,
)


_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]{1,128}")
_HELLO_PATH = "/provider/v1/hello"
_HELLO_REQUEST_BODY_LIMIT = 524_288
_HELLO_RESPONSE_BODY_LIMIT = 65_536
_HELLO_STATUSES = frozenset(
    {200, 400, 401, 403, 404, 408, 413, 414, 431, 500, 503}
)
_EDGE_UNAVAILABLE_STATUSES = frozenset({502, 504})


class RequestDelivery(Enum):
    """How far one request progressed at the HTTPS boundary."""

    NOT_SENT = "not_sent"
    POSSIBLE = "possible"
    RESPONSE_RECEIVED = "response_received"


class ResponseRejection(Enum):
    """Why an HTTP response cannot enter hello receipt parsing."""

    UNDECLARED_STATUS = "undeclared_status"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    CONTENT_ENCODING_NOT_ADMITTED = "content_encoding_not_admitted"
    INVALID_CACHE_CONTROL = "invalid_cache_control"
    INVALID_TOPOLOGY = "invalid_topology"
    INVALID_REQUEST_ID = "invalid_request_id"
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    INVALID_CONTENT_LENGTH = "invalid_content_length"
    RESPONSE_BODY_TOO_LARGE = "response_body_too_large"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A bounded response whose common provider envelope is valid."""

    status: int
    request_id: str | None
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class RequestUnavailable:
    """No valid API response was received for this single send."""

    delivery: RequestDelivery
    cause: BaseException | None = field(default=None, compare=False, repr=False)
    status: int | None = None


@dataclass(frozen=True, slots=True)
class TlsRejected:
    """TLS identity or protocol verification failed before HTTP delivery."""

    delivery: RequestDelivery = RequestDelivery.NOT_SENT
    cause: BaseException | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ResponseRejected:
    """The peer returned an HTTP response outside the pinned hello envelope."""

    reason: ResponseRejection
    status: int | None
    delivery: RequestDelivery = RequestDelivery.RESPONSE_RECEIVED
    cause: BaseException | None = field(default=None, compare=False, repr=False)


HttpOutcome = HttpResponse | RequestUnavailable | TlsRejected | ResponseRejected


@dataclass(frozen=True, slots=True)
class HttpsEndpoint:
    """One canonical NMR API origin and its transport trust policy."""

    origin: str
    expected_topology: str
    connect_timeout_seconds: float
    io_deadline_seconds: float
    ca_file: Path | None = None
    authority: str = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    tls_context: ssl.SSLContext = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_endpoint_config(
            self.origin,
            self.expected_topology,
            self.connect_timeout_seconds,
            self.io_deadline_seconds,
        )
        authority = self.origin.removeprefix("https://")
        host, separator, port_text = authority.rpartition(":")
        if not separator:
            host = authority
            port = 443
        else:
            port = int(port_text)
        ca_file = None if self.ca_file is None else str(Path(self.ca_file))
        context = ssl.create_default_context(cafile=ca_file)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "tls_context", context)


def validate_endpoint_config(
    origin: str,
    expected_topology: str,
    connect_timeout_seconds: float,
    io_deadline_seconds: float,
) -> None:
    """Validate endpoint facts without reading configured TLS trust."""

    prefix = "https://"
    if type(origin) is not str or not origin.startswith(prefix):
        raise ValueError("Provider API origin must be canonical HTTPS")
    if not is_canonical_https_authority(origin[len(prefix) :]):
        raise ValueError("Provider API origin must contain a canonical authority")
    if (
        type(expected_topology) is not str
        or expected_topology not in {"dev-local", "dev", "web"}
    ):
        raise ValueError("Provider API topology must be dev-local, dev, or web")
    _require_positive_finite_timeout(
        connect_timeout_seconds,
        name="Provider API connect timeout",
    )
    _require_positive_finite_timeout(
        io_deadline_seconds,
        name="Provider API I/O deadline",
    )


def send_hello_request(
    *,
    endpoint: HttpsEndpoint,
    request: SignedRequest,
) -> HttpOutcome:
    """Send one hello exactly once and return transport evidence without retry."""

    _validate_hello_request(endpoint, request)

    connection = http.client.HTTPSConnection(
        endpoint.host,
        endpoint.port,
        timeout=endpoint.connect_timeout_seconds,
        context=endpoint.tls_context,
    )
    try:
        try:
            connection.connect()
        except (ssl.SSLCertVerificationError, ssl.SSLError) as error:
            return TlsRejected(cause=error)
        except (OSError, TimeoutError) as error:
            return RequestUnavailable(RequestDelivery.NOT_SENT, error)

        # One deadline covers request transmission, status, headers, and body;
        # resetting a full timeout for each read would admit an endless drip.
        deadline = monotonic() + endpoint.io_deadline_seconds
        transport_socket = connection.sock
        if transport_socket is None:
            return RequestUnavailable(
                RequestDelivery.NOT_SENT,
                RuntimeError("HTTPS connection exposed no transport socket"),
            )
        try:
            _set_remaining_socket_timeout(transport_socket, deadline)
            # The signature covers the raw target and Host value. Supply both
            # explicitly so http.client cannot derive a different wire value.
            connection.putrequest(
                request.method,
                request.raw_target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in request.headers.items():
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(request.body or b"")))
            connection.endheaders(request.body)
            _set_remaining_socket_timeout(transport_socket, deadline)
            response = connection.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            return RequestUnavailable(RequestDelivery.POSSIBLE, error)

        try:
            return _read_response(
                transport_socket=transport_socket,
                response=response,
                expected_topology=endpoint.expected_topology,
                deadline=deadline,
            )
        finally:
            response.close()
    finally:
        connection.close()


def _validate_hello_request(endpoint: HttpsEndpoint, request: SignedRequest) -> None:
    if request.authority != endpoint.authority:
        raise ValueError("Signed request authority does not match Provider API origin")
    if request.method != "POST" or request.path != _HELLO_PATH or request.query:
        raise ValueError("Signed request target does not match provider hello")
    if request.body is None or len(request.body) > _HELLO_REQUEST_BODY_LIMIT:
        raise ValueError("Signed request body does not match provider hello")


def _read_response(
    *,
    transport_socket: socket.socket,
    response: http.client.HTTPResponse,
    expected_topology: str,
    deadline: float,
) -> HttpOutcome:
    status = response.status
    if status in _EDGE_UNAVAILABLE_STATUSES:
        return RequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            status=status,
        )
    if status not in _HELLO_STATUSES:
        return ResponseRejected(ResponseRejection.UNDECLARED_STATUS, status)
    headers = response.getheaders()
    expected_media_type = (
        "application/json" if status == 200 else "application/problem+json"
    )
    if _single_header(headers, "Content-Type") != expected_media_type:
        return ResponseRejected(ResponseRejection.INVALID_CONTENT_TYPE, status)
    if _header_values(headers, "Content-Encoding"):
        return ResponseRejected(
            ResponseRejection.CONTENT_ENCODING_NOT_ADMITTED,
            status,
        )
    if _single_header(headers, "Cache-Control") != "no-store":
        return ResponseRejected(ResponseRejection.INVALID_CACHE_CONTROL, status)
    topology = _single_header(headers, "Nmr-Api-Topology")
    if topology != expected_topology:
        return ResponseRejected(ResponseRejection.INVALID_TOPOLOGY, status)
    request_ids = _header_values(headers, "X-Request-ID")
    request_id = request_ids[0] if len(request_ids) == 1 else None
    if status != 200 and (
        request_id is None or _VISIBLE_ASCII.fullmatch(request_id) is None
    ):
        return ResponseRejected(ResponseRejection.INVALID_REQUEST_ID, status)
    if status == 200 and len(request_ids) > 1:
        return ResponseRejected(ResponseRejection.DUPLICATE_REQUEST_ID, status)

    lengths = _header_values(headers, "Content-Length")
    if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
        return ResponseRejected(ResponseRejection.INVALID_CONTENT_LENGTH, status)
    if lengths and int(lengths[0]) > _HELLO_RESPONSE_BODY_LIMIT:
        return ResponseRejected(ResponseRejection.RESPONSE_BODY_TOO_LARGE, status)
    declared_length = int(lengths[0]) if lengths else None
    body_parts: list[bytes] = []
    body_length = 0
    try:
        while body_length <= _HELLO_RESPONSE_BODY_LIMIT:
            if body_length == declared_length or response.isclosed():
                break
            _set_remaining_socket_timeout(transport_socket, deadline)
            part = response.read1(
                min(65_536, _HELLO_RESPONSE_BODY_LIMIT + 1 - body_length)
            )
            if not part:
                break
            body_parts.append(part)
            body_length += len(part)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        return RequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            error,
            status,
        )
    body = b"".join(body_parts)
    if len(body) > _HELLO_RESPONSE_BODY_LIMIT:
        return ResponseRejected(ResponseRejection.RESPONSE_BODY_TOO_LARGE, status)
    if declared_length is not None and len(body) != declared_length:
        return RequestUnavailable(
            RequestDelivery.RESPONSE_RECEIVED,
            EOFError(
                f"HTTP response ended after {len(body)} of {declared_length} "
                "declared bytes"
            ),
            status=status,
        )
    return HttpResponse(status, request_id, body)


def _header_values(
    headers: list[tuple[str, str]],
    name: str,
) -> list[str]:
    lowered = name.casefold()
    return [value for header, value in headers if header.casefold() == lowered]


def _single_header(headers: list[tuple[str, str]], name: str) -> str | None:
    values = _header_values(headers, name)
    return values[0] if len(values) == 1 else None


def _set_remaining_socket_timeout(
    transport_socket: socket.socket,
    deadline: float,
) -> None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    transport_socket.settimeout(remaining)


def _require_positive_finite_timeout(value: object, *, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
