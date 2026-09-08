"""Interrupt socket I/O at an absolute monotonic deadline.

Buffered HTTP header reads can outlive per-read timeouts. This timer also bounds
worker relay I/O without changing the caller's socket timeout or ownership.
"""

from contextlib import contextmanager
import socket
from threading import Timer
from time import monotonic


@contextmanager
def socket_deadline(transport: socket.socket, deadline: float):
    """Join the interrupter before the caller closes or reuses its socket."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("The HTTP exchange deadline has elapsed")
    timer = Timer(remaining, _interrupt, args=(transport,))
    timer.daemon = True
    timer.start()
    try:
        yield
        if monotonic() >= deadline:
            raise TimeoutError("The HTTP exchange deadline elapsed before completion")
    finally:
        timer.cancel()
        timer.join()


def _interrupt(transport: socket.socket) -> None:
    try:
        transport.shutdown(socket.SHUT_RDWR)
    except OSError:
        # A completed HTTP response may already have closed the transport.
        pass
