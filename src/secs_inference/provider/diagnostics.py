"""Retain bounded exception structure without messages, source text or local values."""

from collections import deque
import builtins
import errno
import os
from pathlib import Path
import socket
import ssl
import traceback



_BUILTIN_EXCEPTION_NAMES = frozenset(
    name for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
)
_SCIENTIFIC_NUMBERS = ("exception_id", "exception_reference", "errno", "tls_verify_code",
                       "omitted_frame_count", "omitted_exception_count")


def scientific_failure_evidence(frame):
    """Project numeric facts and code-owned names without copying child text.

    The original frame remains private. Even plausible filenames, function
    names and TLS labels are untrusted on this channel and are omitted here.
    """
    remaining = 64

    def describe(current):
        nonlocal remaining
        if remaining <= 0:
            return {"truncated": True}
        remaining -= 1
        if type(current) is not dict:
            return {"invalid_structure": True}
        result = {}
        name = current.get("exception_type")
        if type(name) is str and name in _BUILTIN_EXCEPTION_NAMES:
            result["exception_type"] = name
        for field in _SCIENTIFIC_NUMBERS:
            value = current.get(field)
            if type(value) is int and -(2 ** 63) <= value < 2 ** 63 and (field == "errno" or value >= 0):
                result[field] = value
        frames = current.get("frames")
        if type(frames) is list:
            result["omitted_frame_count"] = result.get("omitted_frame_count", 0) + len(frames)
        for relationship in ("cause", "context"):
            if relationship in current:
                result[relationship] = describe(current[relationship])
        members = current.get("exceptions")
        if type(members) is list:
            result["exceptions"] = []
            for index, member in enumerate(members):
                if remaining <= 0:
                    result["omitted_exception_count"] = result.get("omitted_exception_count", 0) + len(members) - index
                    break
                result["exceptions"].append(describe(member))
        omitted = current.keys() - result.keys()
        if type(frames) is list:
            omitted.discard("frames")
        if omitted:
            result["omitted_field_count"] = len(omitted)
        return result

    return describe(frame)


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
            for name in ("library", "reason"):
                value = getattr(current, name, None)
                if isinstance(value, str):
                    evidence[f"tls_{name}"] = value
            if isinstance(current, ssl.SSLCertVerificationError):
                code = getattr(current, "verify_code", None)
                if type(code) is int:
                    evidence["tls_verify_code"] = code
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
