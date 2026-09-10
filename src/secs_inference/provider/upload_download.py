"""Acquire verified Upload bytes without sending provider credentials to a store."""

from base64 import b64encode
from dataclasses import dataclass, field
from hashlib import sha256
import http.client
import logging
import math
import os
from pathlib import Path
import re
import ssl
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import urlsplit

from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.socket_deadline import socket_deadline
from secs_inference.provider.network_errors import network_failure_reason, network_failure_evidence
from secs_inference.provider.connection import https_connection
from secs_inference.provider.configuration_error import ConfigurationError
from secs_inference.provider.http_cleanup import close_http_resource


_LOG = logging.getLogger(__name__)


class UploadDownloadError(RuntimeError):
    """Byte access failed; status, when present, informs acquisition recovery."""

    def __init__(self, reason: str, *, status: int | None = None, diagnostic: dict | None = None):
        super().__init__(f"Cannot obtain Upload bytes: {reason}")
        self.status = status
        self.diagnostic = diagnostic


class UploadUnavailable(UploadDownloadError):
    """No complete transfer was received; a bounded retry may recover it."""


@dataclass(frozen=True, slots=True)
class UploadStore:
    """The operator, not an API-provided URL, grants this network destination."""

    origin: str
    max_upload_bytes: int
    connect_timeout_seconds: float = 10
    ca_file: Path | None = None
    authority: str = field(init=False)
    host: str = field(init=False)
    port: int = field(init=False)
    tls_context: ssl.SSLContext = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        rule = "Upload store origin must be one credential-free HTTPS origin with a valid port (1-65535)"
        if type(self.origin) is not str:
            raise ConfigurationError(rule)
        try:
            parsed = urlsplit(self.origin)
            port = parsed.port
        except ValueError as error:
            raise ConfigurationError(rule) from error
        if (
            not self.origin.isascii() or re.search(r"[\x00-\x20\x7f]", self.origin)
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or self.origin != "https://" + parsed.netloc or port == 0
        ):
            raise ConfigurationError(rule)
        if (type(self.max_upload_bytes) is not int or self.max_upload_bytes <= 0
                or type(self.connect_timeout_seconds) not in (int, float)
                or not math.isfinite(self.connect_timeout_seconds)
                or self.connect_timeout_seconds <= 0):
            raise ConfigurationError("Upload byte limit and connection timeout must be positive")
        object.__setattr__(self, "authority", parsed.netloc)
        object.__setattr__(self, "host", parsed.hostname)
        object.__setattr__(self, "port", port or 443)
        try:
            context = ssl.create_default_context(cafile=self.ca_file)
        except OSError as error:
            reason = ("TLS could not load the CA certificates" if isinstance(error, ssl.SSLError) else
                      os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason")
            raise ConfigurationError(f"Cannot load Upload store TLS trust from {self.ca_file or 'the system CA store'}: {reason}") from error
        object.__setattr__(self, "tls_context", context)


def download_upload(
    *, store: UploadStore, capability: UploadReadCapability,
    directory: Path, deadline: float,
):
    """Return one verified private file; the Attempt owner controls its lifetime.

    There is no redirect, resume or retry here. Acquisition policy may obtain a
    new grant, but must not expose a partly downloaded file to any parser.
    """
    target = "/upload/v1/uploads/" + capability.upload_ref + "/bytes"
    if capability.download_url != store.origin + target:
        raise UploadDownloadError("the grant does not name the configured store and selected Upload")
    if re.fullmatch(r"[\x21-\x7e]+", capability.capability) is None:
        raise UploadDownloadError("the grant bearer cannot be sent as an HTTP header")
    if capability.byte_length > store.max_upload_bytes:
        raise UploadDownloadError(f"the {capability.byte_length}-byte Upload exceeds this provider's {store.max_upload_bytes}-byte per-Upload limit")
    # Successful files must have no destructor-driven deletion: if worker stop
    # cannot be confirmed, they remain until recovery confirms it has exited.
    try:
        output = NamedTemporaryFile(dir=directory, prefix="upload-", delete=False)
    except OSError as error:
        raise _file_error("creation", error) from error
    path = Path(output.name)
    try:
        try:
            _transfer(store, capability, target, output, deadline)
        except BaseException:
            try:
                output.close()
            except OSError as cleanup:
                _LOG.error("%s; preserving the original transfer failure", _file_error("finalization", cleanup))
            raise
        # Closing flushes buffered writes before the verified file is exposed.
        # A failed close must not replace an earlier transfer error above.
        try:
            output.close()
        except OSError as error:
            raise _file_error("finalization", error) from error
        if monotonic() >= deadline:
            raise UploadUnavailable("the acquisition deadline elapsed before the file was ready")
        return path
    except BaseException:
        try:
            path.unlink()
        except OSError as cleanup:
            _LOG.error("%s; the incomplete file may remain in the Attempt workspace", _file_error("removal", cleanup))
        raise


def _file_error(operation: str, error: OSError) -> UploadDownloadError:
    """Keep safe OS evidence at local file effects, never around network I/O."""
    reason = os.strerror(error.errno) if error.errno is not None else "an operating-system error occurred without a recorded reason"
    return UploadDownloadError(f"private input file {operation} failed ({reason})")


def _transfer(store, capability, target, output, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Cannot obtain Upload bytes: the work deadline has elapsed")
    connection = https_connection(
        store.host, store.port, context=store.tls_context,
        timeout=min(store.connect_timeout_seconds, remaining),
    )
    phase = "connecting to the upload store"
    try:
        connection.connect()
        with socket_deadline(connection.sock, deadline):
            phase = "requesting the selected Upload's bytes"
            connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", store.authority)
            connection.putheader("Authorization", "Bearer " + capability.capability)
            connection.endheaders()
            phase = "waiting for the upload store to reply"
            response = connection.getresponse()
            try:
                phase = "receiving the selected Upload's bytes"
                _copy_verified(response, capability, output)
            finally:
                close_http_resource(response, operation=f"Reading Upload {capability.upload_ref}", role="response")
    except ssl.SSLError as error:
        raise UploadDownloadError(f"{phase}: {network_failure_reason(error)}", diagnostic={
            "phase": phase, **network_failure_evidence(error),
        }) from None
    except (OSError, http.client.HTTPException) as error:
        # Exception text may contain a bearer or remote response line. Describe
        # only known transport causes and keep structured evidence private.
        reason = network_failure_reason(error)
        if monotonic() >= deadline:
            reason = "the acquisition deadline elapsed; " + reason
        raise UploadUnavailable(f"{phase}: {reason}; no verified file was made available for analysis", diagnostic={
            "phase": phase, **network_failure_evidence(error),
        }) from None
    finally:
        close_http_resource(connection, operation=f"Reading Upload {capability.upload_ref}", role="connection")


def _copy_verified(response, capability, output):
    if response.status != 200:
        raise UploadDownloadError(f"the store returned HTTP {response.status}", status=response.status)
    digest_bytes = bytes.fromhex(capability.content_hash.removeprefix("sha256:"))
    expected = {
        "Content-Length": str(capability.byte_length),
        "Content-Digest": "sha-256=:" + b64encode(digest_bytes).decode("ascii") + ":",
        "Content-Type": "application/octet-stream",
        "Cache-Control": "no-store",
    }
    for name, value in expected.items():
        if response.headers.get_all(name) != [value]:
            raise UploadDownloadError(f"the store response has a missing or conflicting {name}")
    if response.headers.get_all("Content-Encoding") or response.headers.get_all("Transfer-Encoding"):
        raise UploadDownloadError("the store encoded the bytes instead of sending the declared entity")
    count = 0
    measured = sha256()
    while chunk := response.read1(64 * 1024):
        count += len(chunk)
        try:
            output.write(chunk)
        except OSError as error:
            raise _file_error("write", error) from error
        measured.update(chunk)
    if count != capability.byte_length:
        raise UploadUnavailable("the stream ended before the declared bytes arrived")
    if measured.digest() != digest_bytes:
        raise UploadDownloadError("the received bytes do not match the grant's SHA-256")
