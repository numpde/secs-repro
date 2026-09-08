"""Acquire verified Upload bytes without sending provider credentials to a store."""

from base64 import b64encode
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import http.client
import math
from pathlib import Path
import re
import ssl
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import urlsplit

from secs_inference.provider.job_upload import UploadReadCapability
from secs_inference.provider.socket_deadline import socket_deadline


class UploadDownloadError(RuntimeError):
    """Byte access failed; status, when present, informs acquisition recovery."""

    def __init__(self, reason: str, *, status: int | None = None):
        super().__init__(f"Cannot obtain Upload bytes: {reason}")
        self.status = status


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
        parsed = urlsplit(self.origin)
        if (
            not self.origin.isascii() or re.search(r"[\x00-\x20\x7f]", self.origin)
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or self.origin != "https://" + parsed.netloc
        ):
            raise ValueError("Upload store origin must be one credential-free HTTPS origin")
        if (type(self.max_upload_bytes) is not int or self.max_upload_bytes <= 0
                or not math.isfinite(self.connect_timeout_seconds)
                or self.connect_timeout_seconds <= 0 or parsed.port == 0):
            raise ValueError("Upload byte limit and connection timeout must be positive")
        object.__setattr__(self, "authority", parsed.netloc)
        object.__setattr__(self, "host", parsed.hostname)
        object.__setattr__(self, "port", parsed.port or 443)
        object.__setattr__(self, "tls_context", ssl.create_default_context(cafile=self.ca_file))


@contextmanager
def downloaded_upload(
    *, store: UploadStore, capability: UploadReadCapability,
    directory: Path, deadline: float,
):
    """Keep one verified private file until its Attempt finishes using it.

    There is no redirect, resume or retry here. Acquisition policy may obtain a
    new grant, but must not expose a partly downloaded file to any parser.
    """
    target = "/upload/v1/uploads/" + capability.upload_ref + "/bytes"
    if capability.download_url != store.origin + target:
        raise UploadDownloadError("the grant does not name the configured store and selected Upload")
    if re.fullmatch(r"[\x21-\x7e]+", capability.capability) is None:
        raise UploadDownloadError("the grant bearer cannot be sent as an HTTP header")
    if capability.byte_length > store.max_upload_bytes:
        raise UploadDownloadError("the Upload exceeds the configured acquisition byte limit")
    # NamedTemporaryFile owns both failure cleanup and successful lifetime.
    # No rename or fsync is needed for bytes that never survive the Attempt.
    with NamedTemporaryFile(dir=directory, prefix="upload-") as output:
        _transfer(store, capability, target, output, deadline)
        try:
            output.flush()
        except OSError as error:
            raise UploadDownloadError("the private input file could not be flushed") from error
        if monotonic() >= deadline:
            raise UploadUnavailable("the acquisition deadline elapsed before the file was ready")
        yield Path(output.name)


def _transfer(store, capability, target, output, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Cannot obtain Upload bytes: the work deadline has elapsed")
    connection = http.client.HTTPSConnection(
        store.host, store.port, context=store.tls_context,
        timeout=min(store.connect_timeout_seconds, remaining),
    )
    try:
        connection.connect()
        with socket_deadline(connection.sock, deadline):
            connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", store.authority)
            connection.putheader("Authorization", "Bearer " + capability.capability)
            connection.endheaders()
            response = connection.getresponse()
            try:
                _copy_verified(response, capability, output)
            finally:
                response.close()
    except ssl.SSLError:
        raise UploadDownloadError("the store TLS connection could not be verified or established") from None
    except (OSError, http.client.HTTPException) as error:
        # Low-level HTTP exceptions may include a remote line. Only the class
        # is useful here; neither a bearer nor response text belongs in reports.
        raise UploadUnavailable(f"the transfer did not finish ({type(error).__name__})") from None
    finally:
        connection.close()


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
            raise UploadDownloadError("the private input file could not be written") from error
        measured.update(chunk)
    if count != capability.byte_length:
        raise UploadUnavailable("the stream ended before the declared bytes arrived")
    if measured.digest() != digest_bytes:
        raise UploadDownloadError("the received bytes do not match the grant's SHA-256")
