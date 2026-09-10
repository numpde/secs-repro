"""Keep every failed TCP address while leaving TLS and HTTP to the standard library."""

import http.client
import socket

from secs_inference.provider.network_errors import AddressFailure, ConnectionFailed


def https_connection(host, port, *, timeout, context):
    """Build HTTPS with the usual resolver order and per-address connect timeout."""
    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    # HTTPConnection exposes this instance hook for its TCP connector. Reusing
    # connect() keeps certificate checks, SNI and HTTP behavior in the stdlib.
    connection._create_connection = _connect_tcp
    return connection


def _connect_tcp(address, timeout, source_address=None):
    """Try resolved addresses in order; a later failure must not erase earlier ones."""
    host, port = address
    failures = []
    addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for index, (family, kind, protocol, _, target) in enumerate(addresses):
        transport = None
        try:
            transport = socket.socket(family, kind, protocol)
            transport.settimeout(timeout)
            if source_address:
                transport.bind(source_address)
            transport.connect(target)
            return transport
        except OSError as error:
            if transport is not None:
                try:
                    transport.close()
                except OSError as cleanup:
                    # As in the stdlib, do not continue after failed cleanup;
                    # unlike its final-error projection, retain both failures.
                    failures.append(AddressFailure(family, target, error, cleanup))
                    raise ConnectionFailed(tuple(failures), unattempted_count=len(addresses) - index - 1) from None
            failures.append(AddressFailure(family, target, error))
    if not failures:
        raise OSError("The resolver returned no stream addresses")
    raise ConnectionFailed(tuple(failures))
