"""Retain bounded exception structure without messages, source text or local values."""

from collections import deque
import errno
import os
from pathlib import Path
import socket
import ssl
import traceback

from secs_inference.provider.network_errors import network_failure_evidence


def exception_evidence(error: BaseException, *, boundary_details=None) -> dict:
    """Describe failures and their relationships without reading arbitrary payloads.

    An owning boundary may explicitly supply already-approved structured facts.
    Third-party exception attributes, messages, arguments and notes are not a
    diagnostic API. Limits bound both deep chains and wide exception groups.
    """
    seen = {}
    remaining_nodes = 64

    def describe(current):
        nonlocal remaining_nodes
        if remaining_nodes <= 0:
            return {"truncated": True}
        remaining_nodes -= 1
        if id(current) in seen:
            return {"exception_reference": seen[id(current)]}
        identity = seen[id(current)] = len(seen)
        frames = deque(maxlen=32)
        frame_count = 0
        for frame, line in traceback.walk_tb(current.__traceback__):
            frames.append({"file": Path(frame.f_code.co_filename).name, "line": line, "function": frame.f_code.co_name})
            frame_count += 1
        evidence = {"exception_id": identity, "exception_type": type(current).__name__, "frames": list(frames)}
        if frame_count > len(frames):
            evidence["omitted_frame_count"] = frame_count - len(frames)
        if isinstance(current, OSError) and type(current.errno) is int:
            evidence["errno"] = current.errno
            # TLS and resolver codes are not POSIX errno values. Explain known
            # OS codes without reading exception-supplied strerror or filenames.
            if current.errno in errno.errorcode and not isinstance(current, (ssl.SSLError, socket.gaierror, socket.herror)):
                evidence["reason"] = os.strerror(current.errno)
        if isinstance(current, ssl.SSLError):
            evidence.update(network_failure_evidence(current))
        if boundary_details is not None:
            evidence.update(boundary_details(current))
        if current.__cause__ is not None:
            evidence["cause"] = describe(current.__cause__)
        elif current.__context__ is not None:
            # Suppressing a textual traceback does not erase the earlier
            # failure. Context records chronology, not a claim of causation.
            evidence["context"] = describe(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            members = []
            for index, member in enumerate(current.exceptions):
                if remaining_nodes <= 0:
                    evidence["omitted_exception_count"] = len(current.exceptions) - index
                    break
                members.append(describe(member))
            evidence["exceptions"] = members
        return evidence

    return describe(error)
